"""SQLite 特征缓存 —— 第二次扫描不必重算，大幅提速。

存储内容
--------
* ``books`` 表：每本单行本的元信息（路径、大小、mtime、页数、格式、扫描状态…）
* ``pages`` 表：每本抽样页的特征（pHash / 版式哈希 / 16×16 粗筛图 / 64×64 页图块）
* ``scans`` 表：一次扫描任务的记录（用于统计与排查）

缓存命中判定
------------
``(path, size, mtime, feat_ver)`` 四元组一致才算命中。``feat_ver`` 是**特征算法版本号**，
只要改动会影响特征数值的代码就必须升它，否则旧缓存会被当成新特征误用
（这个坑在视频查重项目里踩过）。

所有数据都在本地，绝不上传。
"""

from __future__ import annotations

import logging
import shutil
import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import __version__, data_dir, norm_path
from .core import FEAT_VER, TILE, TILE_SMALL

log = logging.getLogger("comicdedup")

DB_NAME = "comics.sqlite"


SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

CREATE TABLE IF NOT EXISTS books (
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    kind        TEXT NOT NULL,          -- archive / folder
    fmt         TEXT NOT NULL,
    size        INTEGER NOT NULL,
    mtime       REAL NOT NULL,
    pages       INTEGER NOT NULL,       -- 图片总页数
    sampled     INTEGER NOT NULL,       -- 实际抽到并算了特征的页数
    blanks      INTEGER NOT NULL DEFAULT 0,
    feat_ver    TEXT NOT NULL,
    crop_mode   TEXT NOT NULL DEFAULT '',
    -- 缩略图**直接存进库里**（JPEG 字节），不再落盘成独立文件。
    -- 原因见 _migrate_thumbs 的说明：批量生成 6000+ 个随机哈希命名的 jpg，
    -- 在行为型杀软眼里与勒索软件「加密后重写文件」高度相似，会被报成敲诈病毒。
    thumb_img   BLOB,
    thumb       TEXT NOT NULL DEFAULT '',   -- 兼容旧版：曾是 thumbs 目录下的文件名
    status      TEXT NOT NULL DEFAULT 'ok',   -- ok / empty / error
    error       TEXT NOT NULL DEFAULT '',
    scanned_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_books_path ON books(path);

CREATE TABLE IF NOT EXISTS pages (
    book_id   INTEGER NOT NULL,
    idx       INTEGER NOT NULL,          -- 页序号（自然排序后的 0 基下标）
    phash     INTEGER NOT NULL,
    bhash32   INTEGER NOT NULL,
    aspect    REAL NOT NULL,
    std       REAL NOT NULL,
    ink       REAL NOT NULL,
    px        INTEGER NOT NULL DEFAULT 0,
    blank     INTEGER NOT NULL DEFAULT 0,
    map16     BLOB NOT NULL,          -- 16*16 原始字节（只有 256 B，不压缩）
    tile      BLOB NOT NULL,          -- 64*64 页图块，zlib 压缩
    PRIMARY KEY (book_id, idx),
    FOREIGN KEY (book_id) REFERENCES books(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS scans (
    id         INTEGER PRIMARY KEY,
    root       TEXT NOT NULL,
    started_at REAL NOT NULL,
    ended_at   REAL,
    n_books    INTEGER DEFAULT 0,
    n_groups   INTEGER DEFAULT 0,
    note       TEXT DEFAULT ''
);

CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT
);
"""


class FeatureCache:
    """特征缓存。线程安全（内部一把锁串行化所有写操作）。"""

    FLUSH_ROWS = 400
    """页特征缓冲攒到这么多行就落盘一次。

    多线程扫描时这个值还有第二层含义：进程若被强杀，**最多**只有这么多行（≈10 本）
    的页特征还没提交，重跑时会被重算 —— 所以它同时也是「中断代价的上限」。
    """

    def __init__(self, path: Optional[Path] = None):
        p = Path(path) if path else (data_dir() / DB_NAME)
        p.parent.mkdir(parents=True, exist_ok=True)
        self.path = p
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(str(p), check_same_thread=False, timeout=30.0)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self.conn.commit()
        self._pending_pages: list = []
        with self._lock:
            self._ensure_columns()
            self._migrate_thumbs()

    # -------------------------------------------------- 建库后的演进

    def _ensure_columns(self):
        """给**旧库**补上后加的列（CREATE TABLE IF NOT EXISTS 不会改已有表）。"""
        with self._lock:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(books)")}
            if "thumb_img" not in cols:
                self.conn.execute("ALTER TABLE books ADD COLUMN thumb_img BLOB")
                self.conn.commit()

    def _migrate_thumbs(self):
        """把散落在 ``thumbs/`` 目录里的旧缩略图搬进数据库，然后删掉那个目录。

        **为什么必须搬走**：早期版本每扫一本书就往 ``%LOCALAPPDATA%\\ComicDedup\\thumbs\\``
        写一个 jpg，文件名是 ``sha1(路径)[:20]``（例如 ``00074a51afb55c6cd78e.jpg``）。
        3000 本就是 6000+ 个**随机哈希命名**的图片文件，短时间内批量生成 ——
        这个行为模式与勒索软件「加密文档后重写」高度重合，360 等国产杀软的
        行为型引擎（QVM）会直接报成**敲诈病毒**。程序本身当然没有任何加密行为，
        但行为指纹撞上了就是撞上了，改行为比解释更有效。

        缩略图只是界面上的一张封面预览（约 19 KB），存进 SQLite 完全没有压力，
        而且顺带解决了另外两个老问题：旧缩略图**只增不减**（清缓存也删不掉），
        以及几千个小文件带来的目录开销。
        """
        tdir = data_dir() / "thumbs"
        if not tdir.is_dir():
            return
        try:
            with self._lock:
                rows = self.conn.execute(
                    "SELECT id, thumb FROM books WHERE (thumb_img IS NULL OR thumb_img = X'') "
                    "AND thumb != ''").fetchall()
            if not rows:
                n = 0
            else:
                n = 0
                for r in rows:
                    fp = tdir / r["thumb"]
                    try:
                        data = fp.read_bytes()
                    except OSError:
                        continue
                    if not data:
                        continue
                    with self._lock:
                        self.conn.execute("UPDATE books SET thumb_img=? WHERE id=?",
                                          (sqlite3.Binary(data), r["id"]))
                    n += 1
                with self._lock:
                    self.conn.commit()
            # 迁移完就不再需要这个目录了：里面只有我们自己生成的缓存图片。
            # 留着既占空间，又会让杀软的行为引擎继续盯着。
            # 只有当「该搬的都搬成了」才删目录。若一个都没搬成功（文件读不到、
            # 权限问题等），保留现场 —— 缩略图丢了不影响查重，但没必要平白丢掉。
            if rows and n == 0:
                log.warning("缩略图一张也没搬成，保留原目录 %s", tdir)
                return
            shutil.rmtree(tdir, ignore_errors=True)
            log.info("缩略图已迁入数据库 %d 张，并删除旧缓存目录 %s", n, tdir)
        except Exception as e:                       # 迁移失败不影响主流程
            log.warning("缩略图迁移失败（忽略）：%s", e)

    # -------------------------------------------------- 元信息

    def meta_get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        with self._lock:
            row = self.conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        return row["v"] if row else default

    def meta_set(self, key: str, value: str):
        with self._lock:
            self.conn.execute("INSERT INTO meta(k,v) VALUES(?,?) "
                              "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, str(value)))
            self.conn.commit()

    # -------------------------------------------------- 命中判定

    def lookup(self, path: str, size: int, mtime: float, crop_mode: str) -> Optional[dict]:
        """查缓存。命中返回字典（含 pages 行），否则 None。"""
        path = norm_path(path)
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM books WHERE path=? AND size=? AND mtime=? AND feat_ver=? "
                "AND crop_mode=?",
                (path, int(size), float(mtime), FEAT_VER, crop_mode)).fetchone()
            if row is None:
                return None
            pages = self.conn.execute(
                "SELECT idx,phash,bhash32,aspect,std,ink,px,blank,map16,tile FROM pages "
                "WHERE book_id=? ORDER BY idx", (row["id"],)).fetchall()
        d = dict(row)
        d["pages"] = [dict(p) for p in pages]
        return d

    def touch(self, book_id: int):
        with self._lock:
            self.conn.execute("UPDATE books SET scanned_at=? WHERE id=?",
                              (time.time(), book_id))
            self.conn.commit()

    # -------------------------------------------------- 写入

    def put_book(self, path: str, kind: str, fmt: str, size: int, mtime: float,
                 pages: int, sampled: int, blanks: int, crop_mode: str,
                 thumb: bytes = b"", status: str = "ok", error: str = "") -> int:
        """写入/更新一本书的元信息。

        ``thumb`` 现在是**缩略图的 JPEG 字节**（直接存进库，不再落盘成文件）。
        """
        path = norm_path(path)
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO books(path,kind,fmt,size,mtime,pages,sampled,blanks,feat_ver,"
                "crop_mode,thumb_img,status,error,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, fmt=excluded.fmt, "
                "size=excluded.size, mtime=excluded.mtime, pages=excluded.pages, "
                "sampled=excluded.sampled, blanks=excluded.blanks, feat_ver=excluded.feat_ver, "
                "crop_mode=excluded.crop_mode, thumb_img=excluded.thumb_img, status=excluded.status, "
                "error=excluded.error, scanned_at=excluded.scanned_at",
                (path, kind, fmt, int(size), float(mtime), int(pages), int(sampled),
                 int(blanks), FEAT_VER, crop_mode,
                 sqlite3.Binary(thumb) if thumb else None, status, error, time.time()))
            bid = cur.lastrowid
            if not bid or bid == 0:
                r = self.conn.execute("SELECT id FROM books WHERE path=?", (path,)).fetchone()
                bid = r["id"]
                self.conn.execute("DELETE FROM pages WHERE book_id=?", (bid,))
            self.conn.commit()
        return int(bid)

    def put_pages(self, book_id: int, feats: Sequence, buffered: bool = True):
        """写入页特征。``buffered=True`` 时先攒着，由 ``flush`` 批量提交（快很多）。"""
        rows = [(int(book_id), int(f.idx), int(f.phash), int(f.bhash32), float(f.aspect),
                 float(f.std), float(f.ink), int(getattr(f, "px", 0)),
                 1 if f.blank else 0, bytes(f.map16), zlib.compress(f.tile, 6))
                for f in feats]
        if not buffered:
            with self._lock:
                self.conn.executemany(
                    "INSERT OR REPLACE INTO pages(book_id,idx,phash,bhash32,aspect,std,ink,px,"
                    "blank,map16,tile) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
                self.conn.commit()
            return
        with self._lock:
            self._pending_pages.extend(rows)
            need = len(self._pending_pages) >= self.FLUSH_ROWS
        if need:
            self.flush()

    def flush(self):
        """把缓冲里的页特征落盘。

        ⚠ 这四件事必须**整体**在同一把锁里完成：取缓冲、清缓冲、写库、提交。

        原先是把 `rows, self._pending_pages = self._pending_pages, []` 写在锁外 ——
        单线程完全没问题，但特征提取改成多线程跑之后，两个线程可能同时看到
        「缓冲非空」并各自拿到**同一个 list**，于是同一批页被 executemany 插入两遍；
        更糟的是可能拿到一个正在被别的线程 append 的列表。
        （锁是可重入的 RLock，put_pages 里持锁调用本函数也安全。）
        """
        with self._lock:
            if not self._pending_pages:
                return
            rows, self._pending_pages = self._pending_pages, []
            self.conn.executemany(
                "INSERT OR REPLACE INTO pages(book_id,idx,phash,bhash32,aspect,std,ink,px,"
                "blank,map16,tile) VALUES(?,?,?,?,?,?,?,?,?,?,?)", rows)
            self.conn.commit()

    # -------------------------------------------------- 读取（分组用）

    def all_pages(self, need_tile: bool = False) -> list:
        """读出所有页特征。返回 [(book_path, book_id, page_row_dict), ...]。

        只读**当前特征算法版本**（``FEAT_VER``）的书 —— 缓存里可能还残留旧版本的
        特征（比如采样方式改版后旧缓存没清），若把新旧两套一起读进来，同一本书会
        因为「旧版本特征 + 新版本特征」并存而自己跟自己判重。
        """
        cols = "p.idx,p.phash,p.bhash32,p.aspect,p.std,p.ink,p.px,p.blank,p.map16"
        if need_tile:
            cols += ",p.tile"
        with self._lock:
            rows = self.conn.execute(
                f"SELECT b.path AS bpath, b.id AS bid, b.size AS bsize, b.pages AS bpages, "
                f"b.kind AS bkind, b.fmt AS bfmt, b.status AS bstatus, {cols} "
                f"FROM pages p JOIN books b ON b.id=p.book_id "
                f"WHERE p.blank=0 AND b.status='ok' AND b.feat_ver=? "
                f"ORDER BY b.id, p.idx", (FEAT_VER,)).fetchall()
        return [dict(r) for r in rows]

    def book_meta(self, with_thumb: bool = True) -> list:
        """所有书的元信息。``with_thumb=False`` 时不取缩略图字节（省内存）。"""
        cols = ("id,path,kind,fmt,size,mtime,pages,sampled,blanks,"
                + ("thumb_img,thumb," if with_thumb else "")
                + "status,error")
        with self._lock:
            rows = self.conn.execute(
                f"SELECT {cols} FROM books WHERE feat_ver=? ORDER BY path",
                (FEAT_VER,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["thumb_img"] = bytes(d["thumb_img"]) if d.get("thumb_img") else b""
            out.append(d)
        return out

    def tiles_for_books(self, book_ids: Sequence[int]) -> dict:
        """取指定书的页图块：{book_id: [(idx, tile_bytes, map16, phash, px), ...]}。

        过滤条件与 :meth:`all_pages` 保持一致（``blank=0`` 且 ``books.status='ok'``），
        这样按书拼接出来的页序与页特征矩阵**逐行对齐** —— 候选预筛会把图块描述子
        铺进一个大矩阵，一旦两者集合不同就会错位。
        """
        if not book_ids:
            return {}
        q = ",".join("?" for _ in book_ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT p.book_id, p.idx, p.tile, p.map16, p.phash, p.px "
                f"FROM pages p JOIN books b ON b.id=p.book_id "
                f"WHERE p.book_id IN ({q}) AND p.blank=0 AND b.status='ok' "
                f"AND b.feat_ver=? "
                f"ORDER BY p.book_id, p.idx", tuple(book_ids) + (FEAT_VER,)).fetchall()
        out: dict = {}
        for r in rows:
            out.setdefault(int(r["book_id"]), []).append(
                (int(r["idx"]), zlib.decompress(r["tile"]),
                 bytes(r["map16"]), int(r["phash"]), int(r["px"] or 0)))
        return out

    # -------------------------------------------------- 统计 / 清理

    def stats(self) -> dict:
        with self._lock:
            b = self.conn.execute("SELECT COUNT(*) c, COALESCE(SUM(pages),0) p, "
                                  "COALESCE(SUM(sampled),0) s FROM books WHERE feat_ver=?",
                                  (FEAT_VER,)).fetchone()
            pg = self.conn.execute(
                "SELECT COUNT(*) c FROM pages p JOIN books b ON b.id=p.book_id "
                "WHERE b.feat_ver=?", (FEAT_VER,)).fetchone()
            err = self.conn.execute(
                "SELECT COUNT(*) c FROM books WHERE status!='ok' AND feat_ver=?",
                (FEAT_VER,)).fetchone()
        return {"books": b["c"], "pages": b["p"], "sampled": b["s"],
                "page_rows": pg["c"], "bad": err["c"], "db": str(self.path)}

    def clear(self, only_history: bool = False):
        """清空缓存。``only_history=True`` 只清扫描记录，保留特征。"""
        with self._lock:
            if only_history:
                self.conn.execute("DELETE FROM scans")
            else:
                self.conn.execute("DELETE FROM pages")
                self.conn.execute("DELETE FROM books")
            self.conn.commit()
            self.conn.execute("VACUUM")

    def start_scan(self, root: str) -> int:
        with self._lock:
            # 清掉**非当前特征版本**的旧特征：算法/采样改版后（feat_ver 升级），
            # 旧缓存对新版本毫无用处，只会占空间、还可能被误读（见 all_pages 的说明）。
            # 删除前先看看有没有残留，有就一并清掉。
            n_old = self.conn.execute(
                "SELECT COUNT(*) c FROM books WHERE feat_ver != ?", (FEAT_VER,)).fetchone()["c"]
            if n_old:
                self.conn.execute("DELETE FROM pages WHERE book_id IN "
                                  "(SELECT id FROM books WHERE feat_ver != ?)", (FEAT_VER,))
                self.conn.execute("DELETE FROM books WHERE feat_ver != ?", (FEAT_VER,))
                log.info("清掉旧特征版本的缓存 %d 本（feat_ver 已升级）", n_old)
            self._normalize_paths()
            cur = self.conn.execute("INSERT INTO scans(root,started_at) VALUES(?,?)",
                                    (root, time.time()))
            self.conn.commit()
            return int(cur.lastrowid)

    def _normalize_paths(self):
        """把库里已有的路径统一成盘符形式，并合并因此撞在一起的同名记录。

        早期命令行用 ``Path.resolve()`` 展开网络盘符，留下了一批 UNC 路径
        （``\\\\NAS\\漫画\\a.zip``）；界面里填的是盘符（``M:\\a.zip``）。
        同一本书两种写法 = 两条记录 = 分组时自己跟自己判重。这里在每次扫描开始前
        把存量记录一并收敛，重复者只留最近扫过的那一条。
        """
        rows = self.conn.execute("SELECT id, path, scanned_at FROM books").fetchall()
        if not rows:
            return
        changed = 0
        keep: dict = {}
        drop: list = []
        for r in rows:
            new = norm_path(r["path"])
            if new != r["path"]:
                changed += 1
            prev = keep.get(new)
            if prev is None:
                keep[new] = (r["id"], float(r["scanned_at"] or 0))
            else:
                # 同一本书两种写法 → 丢掉扫得较早的那条
                if float(r["scanned_at"] or 0) >= prev[1]:
                    drop.append(prev[0])
                    keep[new] = (r["id"], float(r["scanned_at"] or 0))
                else:
                    drop.append(r["id"])
        for bid in drop:
            self.conn.execute("DELETE FROM pages WHERE book_id=?", (bid,))
            self.conn.execute("DELETE FROM books WHERE id=?", (bid,))
        for new, (bid, _t) in keep.items():
            self.conn.execute("UPDATE books SET path=? WHERE id=?", (new, bid))
        self.conn.commit()
        if changed or drop:
            log.info("路径归一：改写 %d 条，合并重复记录 %d 条", changed, len(drop))

    def end_scan(self, scan_id: int, n_books: int, n_groups: int, note: str = ""):
        with self._lock:
            self.conn.execute("UPDATE scans SET ended_at=?,n_books=?,n_groups=?,note=? "
                              "WHERE id=?", (time.time(), n_books, n_groups, note, scan_id))
            self.conn.commit()

    def close(self):
        try:
            self.flush()
            self.conn.commit()
            self.conn.close()
        except Exception:
            pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
