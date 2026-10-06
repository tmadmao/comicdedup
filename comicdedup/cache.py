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

所有数据都在本机，绝不上传。
"""

from __future__ import annotations

import sqlite3
import threading
import time
import zlib
from pathlib import Path
from typing import Iterable, Optional, Sequence

from . import __version__, data_dir
from .core import FEAT_VER, TILE, TILE_SMALL

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
    thumb       TEXT NOT NULL DEFAULT '',
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
                 thumb: str = "", status: str = "ok", error: str = "") -> int:
        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO books(path,kind,fmt,size,mtime,pages,sampled,blanks,feat_ver,"
                "crop_mode,thumb,status,error,scanned_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, fmt=excluded.fmt, "
                "size=excluded.size, mtime=excluded.mtime, pages=excluded.pages, "
                "sampled=excluded.sampled, blanks=excluded.blanks, feat_ver=excluded.feat_ver, "
                "crop_mode=excluded.crop_mode, thumb=excluded.thumb, status=excluded.status, "
                "error=excluded.error, scanned_at=excluded.scanned_at",
                (path, kind, fmt, int(size), float(mtime), int(pages), int(sampled),
                 int(blanks), FEAT_VER, crop_mode, thumb, status, error, time.time()))
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
        """读出所有页特征。返回 [(book_path, book_id, page_row_dict), ...]。"""
        cols = "p.idx,p.phash,p.bhash32,p.aspect,p.std,p.ink,p.px,p.blank,p.map16"
        if need_tile:
            cols += ",p.tile"
        with self._lock:
            rows = self.conn.execute(
                f"SELECT b.path AS bpath, b.id AS bid, b.size AS bsize, b.pages AS bpages, "
                f"b.kind AS bkind, b.fmt AS bfmt, b.status AS bstatus, {cols} "
                f"FROM pages p JOIN books b ON b.id=p.book_id "
                f"WHERE p.blank=0 AND b.status='ok' ORDER BY b.id, p.idx").fetchall()
        return [dict(r) for r in rows]

    def book_meta(self) -> list:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id,path,kind,fmt,size,mtime,pages,sampled,blanks,thumb,status,error "
                "FROM books ORDER BY path").fetchall()
        return [dict(r) for r in rows]

    def tiles_for_books(self, book_ids: Sequence[int]) -> dict:
        """取指定书的页图块：{book_id: [(idx, tile_bytes), ...]}。"""
        if not book_ids:
            return {}
        q = ",".join("?" for _ in book_ids)
        with self._lock:
            rows = self.conn.execute(
                f"SELECT book_id, idx, tile, map16, phash, px FROM pages WHERE book_id IN ({q}) "
                f"AND blank=0 ORDER BY book_id, idx", tuple(book_ids)).fetchall()
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
                                  "COALESCE(SUM(sampled),0) s FROM books").fetchone()
            pg = self.conn.execute("SELECT COUNT(*) c FROM pages").fetchone()
            err = self.conn.execute("SELECT COUNT(*) c FROM books WHERE status!='ok'").fetchone()
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
            cur = self.conn.execute("INSERT INTO scans(root,started_at) VALUES(?,?)",
                                    (root, time.time()))
            self.conn.commit()
            return int(cur.lastrowid)

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
