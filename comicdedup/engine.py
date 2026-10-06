"""扫描引擎：目录扫描 → 抽页提特征 → 多页比对分组 → 导出 / 安全删除。

流程
----
1. **目录扫描**：递归找压缩包与漫画图片文件夹（``archives.scan_books``）。
2. **抽页提特征**：每本按「锚点 + 窗口」采样若干内页，压缩包内图片**在内存里**读取解码，
   算完特征即可丢弃原图；结果写进 SQLite 缓存（第二次扫描直接命中，秒过）。
   损坏压缩包/损坏图片只记日志并跳过，程序不崩。
3. **候选筛选**：把所有抽样页的 16×16 粗筛图拼成矩阵，按书分批做矩阵乘法，
   统计「每两本书之间有多少页对相似」→ 得到候选书对（避免 O(n²) 全量精比）。
4. **精比**：候选书对逐页做**平移不变**精比，融合 pHash，得到页对分数矩阵，
   贪心一对一匹配后统计命中页数。
5. **分组**：命中页数 ≥ K 且命中率 ≥ 下限 → 判重；并查集合并，合并前做**组代表互验**
   （防止「同系列各卷共享封面/预告页」把整个系列连成一串）。
"""

from __future__ import annotations

import csv
import logging
import os
import shutil
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from bisect import bisect_left
from collections import deque
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Callable, Iterable, Optional, Sequence

import numpy as np

from . import data_dir
from . import archives as A
from . import core
from .cache import FeatureCache

log = logging.getLogger("comicdedup")


# ------------------------------------------------------------------ 参数


PRESETS = {
    # 名称: (采样块数, 每块半宽, 每本最多抽多少页)
    #
    # ⚠ 采样方式在 v1.1 改过：从「8 个锚点各 ±2 页」改成「4 个块各 13 页」。
    # 原因见 sample_indices 的说明 —— 块越长，越能容忍两个版本之间**页数不一样**。
    "快速": (3, 3, 21),
    "标准": (4, 6, 52),
    "彻底": (6, 8, 102),
    "全页": (0, 0, 0),          # 0 = 所有页
}
PRESET_HELP = {
    "快速": "每本抽 20 页左右（3 个块）。只适合先跑一遍找「同源改名/换格式」这类重复。",
    "标准": "每本抽 52 页以内（4 个块，每块 13 页）。能覆盖「扫描版 vs 官方DL版」，推荐。",
    "彻底": "每本抽 102 页以内（6 个块，每块 17 页）。两版页数差得多时更稳，但耗时长。",
    "全页": "每一页都算。最准最慢（3000 本 × 180 页 ≈ 54 万页，首次可能要好几个小时）。",
}


@dataclass
class ScanSettings:
    root: Path = Path(".")
    # --- 采样（默认值与「标准」预设一致：4 块 × 每块 13 页、上限 52。
    #     语义见 sample_indices —— anchors 是**块数**、window 是**块半径**。
    #     绕过 from_preset 直接构造时也必须拿到块采样，不能是旧稀疏锚点。）
    preset: str = "标准"
    anchors: int = 4
    window: int = 6
    max_pages: int = 52
    # --- 预处理
    crop_mode: str = core.DEFAULT_CROP
    do_deskew: bool = True
    # --- 缓存 / 缩略图
    use_cache: bool = True
    make_thumbs: bool = True
    # --- 判重
    page_thr: float = 0.62       # 页级相似度阈值
    min_pages: int = 3           # 至少多少页成链才算同一本
    ratio_thr: float = 0.25      # 成链页数 / 较短那本的抽样页数（防同系列连环并组）
    in_order: float = 0.6        # 成链页数 / 命中页数下限（拦页序乱跳的噪声）
    coarse_thr: float = 0.70     # 候选预筛阈值（作用在 16x16 粗筛图上，见 GroupSettings）
    pre_min: int = 5             # 预筛：命中页数 + 成链页数都要达到这个数才进精比
    pre_ratio: float = 0.2       # 页数少的书的预筛兜底比例
    # --- 其它
    max_books: int = 0           # 0 = 不限（调试用）
    threads: int = 0             # 工作线程数：0 = 自动（min(8, CPU 逻辑核数)）

    @classmethod
    def from_preset(cls, name: str, **kw) -> "ScanSettings":
        a, w, m = PRESETS.get(name, PRESETS["标准"])
        return cls(preset=name, anchors=a, window=w, max_pages=m, **kw)


@dataclass
class ScanStats:
    n_books: int = 0
    n_cached: int = 0
    n_scanned: int = 0
    n_pages: int = 0
    n_bad: int = 0
    elapsed: float = 0.0
    skipped: int = 0


# ------------------------------------------------------------------ 采样


def sample_indices(page_count: int, anchors: int, window: int, max_pages: int) -> list:
    """决定每本抽哪几页。

    策略：在整本书上均匀放 ``anchors`` 个**块**，每块连续取 ``2*window+1`` 页。
    第 0 页（封面）不抽 —— 需求明确要求不要只比封面，而且封面往往是后配的。

    **为什么是「块」而不是「稀疏锚点」（v1.1 的关键改动）**

    两个版本之间只要页数不一样，对应页的**绝对页号**就会越差越远：页数差 ΔP 时，
    全书 85% 处的对应页偏移 ≈ 0.85·ΔP。稀疏采样（每处只抽 1 页）要靠「窗口」去
    兜这个偏移，窗口一超就整块落空。

    实测（3067 本真实单行本）：旧方案（8 锚点 × ±2 页）在 ΔP ≈ 12 页时，
    8 个块里只有前 2~3 个能对上，一本 242 页的书只配出 4 页 → 判重失败。
    而统计工具**已判重**的 266 对书，页数差中位数仅 0.5%、94% 都在 5% 以内 ——
    说明旧方案的召回天花板就是「两版页数差 ≈ 2%」。

    块越长，能容忍的偏移越大（块长 13 页 ≈ 容忍 13 页偏移）。在同样的页数预算下，
    「少而长」的块远胜「多而短」的锚点：ΔP=12、总预算 52 页时，
    块长 13（4 块）能配上约 28 页，块长 5（10 块）只能配上 2~6 页。

    块中心均匀落在 (0,1) 开区间内，避免把块贴到书的最前/最后一页 ——
    因为「另一版多出来的页」通常就堆在书的头尾两端。
    """
    if page_count <= 0:
        return []
    if page_count <= 3:
        # 超短书：能抽的内页全抽（与主路径一致，第 0 页封面不抽；
        # 单页书没得选，只能抽它）
        return list(range(1, page_count)) or [0]
    if anchors <= 0:
        return list(range(page_count))
    body = page_count - 1
    if anchors == 1:
        centers = [1 + body // 2]
    else:
        centers = [1 + int(round((k + 0.5) * body / anchors)) for k in range(anchors)]
    idxs = set()
    for c in centers:
        for d in range(-window, window + 1):
            j = c + d
            if 1 <= j < page_count:
                idxs.add(j)
    out = sorted(idxs)
    if max_pages and len(out) > max_pages:
        # 超预算就均匀抽稀（块会变短，容忍偏移的能力下降，但至少覆盖得住全书）
        step = len(out) / float(max_pages)
        out = sorted({out[min(len(out) - 1, int(i * step))] for i in range(max_pages)})
    return out


# ------------------------------------------------------------------ 扫描


class ScanWorker:
    """可暂停 / 可中断的扫描任务。GUI 里跑在 QThread，命令行里直接跑。"""

    def __init__(self, settings: ScanSettings, cache: FeatureCache,
                 on_progress: Optional[Callable] = None,
                 on_log: Optional[Callable] = None):
        self.s = settings
        self.cache = cache
        self.on_progress = on_progress
        self.on_log = on_log
        self._pause = threading.Event()
        self._stop = threading.Event()
        self._stat_lock = threading.Lock()   # 保护 stats 计数（多线程并发累加）
        self._log_lock = threading.Lock()    # 串行化日志/进度输出，避免整行被交错
        self.stats = ScanStats()
        self.errors: list = []
        self.thumb_dir = data_dir() / "thumbs"
        self.thumb_dir.mkdir(parents=True, exist_ok=True)

    # ---------------- 控制
    def pause(self):
        self._pause.set()

    def resume(self):
        self._pause.clear()

    def stop(self):
        self._stop.set()
        self._pause.clear()

    @property
    def paused(self) -> bool:
        return self._pause.is_set()

    def _wait_if_paused(self):
        while self._pause.is_set() and not self._stop.is_set():
            time.sleep(0.15)

    def _bump(self, field_name: str, n: int = 1):
        """原子地累加统计计数。

        改成多线程扫描后，`stats.n_scanned += 1` 这种「读-改-写」会丢更新
        —— 两个线程可能同时读到同一个旧值、各自加一、只写回一次。
        所以所有计数统一走这里加锁。
        """
        with self._stat_lock:
            setattr(self.stats, field_name, getattr(self.stats, field_name) + n)

    def snapshot(self) -> ScanStats:
        """取一份计数的即时快照（进度回调用，避免读到写了一半的值）。"""
        with self._stat_lock:
            return replace(self.stats)

    def _workers(self) -> int:
        """扫描阶段的工作线程数。``threads<=0`` 表示自动。

        默认取 ``min(8, CPU 逻辑核数)``：这个阶段是「解码 + 十几个小算子」的混合负载，
        本地盘时基本吃满 CPU，所以线程数不宜超过核数太多；读 NAS 时会有相当比例的时间
        花在网络 I/O 上，适当调高（比如 1.5 倍核数）能把 I/O 等待重叠掉。
        """
        n = int(self.s.threads or 0)
        if n <= 0:
            n = max(2, min(8, os.cpu_count() or 4))
        return max(1, min(64, n))

    def _say(self, msg: str, level: str = "info"):
        # 多线程下日志来自不同线程，必须串行化，否则两行会交错成乱码
        with self._log_lock:
            if self.on_log:
                try:
                    self.on_log(level, msg)
                except Exception:
                    pass
            if level == "error":
                log.error(msg)
            elif level == "warn":
                log.warning(msg)
            else:
                log.info(msg)

    def _progress(self, done: int, total: int, path: str):
        if not self.on_progress:
            return
        try:
            self.on_progress(done, total, path, self.snapshot())
        except Exception:
            pass

    # ---------------- 主流程
    def run(self) -> ScanStats:
        t0 = time.time()
        s = self.s
        self._say(f"开始扫描：{s.root}")
        backends = A.available_backends()
        usable = [k for k in ("sevenzip", "unrar", "bsdtar", "py7zr", "rarfile") if backends.get(k)]
        self._say(f"可用解压后端：zipfile(内置) + {', '.join(usable) if usable else '（无外部程序）'}")

        books = A.scan_books(s.root, progress=None, should_stop=self._stop.is_set)
        if s.max_books:
            books = books[: s.max_books]
        total = len(books)
        self.stats.n_books = total
        self._say(f"发现单行本 {total} 本（压缩包 + 图片文件夹）")
        if not total:
            self.stats.elapsed = time.time() - t0
            return self.stats

        nw = self._workers()
        self._say(f"扫描线程数：{nw}（共 {total} 本）")
        if nw > 1:
            # 我们自己已经在「页」这一层并行了，绝不能让 OpenCV 内部再各开一套线程
            # —— 否则 nw × OpenCV 自己的线程数 会互相抢核（6×6=36），反而更慢。
            try:
                import cv2
                cv2.setNumThreads(1)
            except Exception:
                pass

        done = processed = 0
        try:
            # 任务一次性全提交，用 as_completed 收结果。
            # 每个任务开头都会检查 _stop，所以按下「停止」后，仍在排队里的任务
            # 会立刻空转返回，不会继续啃剩下的几千本。
            with ThreadPoolExecutor(max_workers=nw, thread_name_prefix="scan") as ex:
                futures = {ex.submit(self._scan_task, b): b for b in books}
                for fut in as_completed(futures):
                    done += 1
                    try:
                        if fut.result():
                            processed += 1
                    except Exception:
                        pass                    # 任务体内部已兜底，这里是双保险
                    self._progress(done, total, str(futures[fut].path))
        finally:
            # 正常结束、抛异常、Ctrl+C —— 缓冲里的页特征都必须落盘。
            # 否则这些书会留下「有记录、零页」的不完整行（重跑虽会自愈，但白算一遍）。
            self.cache.flush()
        self.stats.skipped = max(0, total - processed)
        if self.stats.skipped:
            self._say(f"已中断，{self.stats.skipped} 本未处理", "warn")
        self.stats.elapsed = time.time() - t0
        self._say(f"扫描完成：{self.stats.n_books} 本，"
                  f"新算 {self.stats.n_scanned} 本 / 缓存命中 {self.stats.n_cached} 本，"
                  f"共 {self.stats.n_pages} 页特征，异常 {self.stats.n_bad} 本，"
                  f"耗时 {self.stats.elapsed:.1f} 秒")
        return self.stats

    # ---------------- 单本
    def _scan_task(self, b: A.BookEntry) -> bool:
        """一个工作单元（可能跑在工作线程里）。返回 False 表示因「停止」被跳过。

        专门抽成一个方法、而不是把逻辑塞进 ``submit()`` 的 lambda，是为了让异常
        边界清楚：任何一本书的失败都只影响它自己，绝不把整个扫描带崩。
        """
        if self._stop.is_set():
            return False
        self._wait_if_paused()
        if self._stop.is_set():
            return False
        try:
            self._scan_one(b)
        except Exception as e:                      # 单本失败绝不影响全局
            self._bump("n_bad")
            self._record_error(b, f"{type(e).__name__}: {e}")
        return True

    def _record_error(self, b: A.BookEntry, msg: str):
        self.errors.append((str(b.path), msg))
        self._say(f"跳过（{b.path.name}）：{msg}", "error")
        try:
            A.stat_book(b)
            self.cache.put_book(str(b.path), b.kind, b.fmt, b.size, b.mtime, 0, 0, 0,
                                self.s.crop_mode, thumb="", status="error", error=msg[:300])
        except Exception:
            pass

    def _scan_one(self, b: A.BookEntry):
        s = self.s
        A.stat_book(b)
        key = str(b.path)

        if s.use_cache:
            hit = self.cache.lookup(key, b.size, b.mtime, s.crop_mode)
            if hit and hit.get("pages") and hit.get("status") == "ok":
                self.cache.touch(int(hit["id"]))
                self._bump("n_cached")
                self._bump("n_pages", len(hit["pages"]))
                return
            if hit and hit.get("status") == "error":
                # 上次就是坏的；文件没变就不再重试，避免每次扫描都卡同一个坏档
                self._bump("n_cached")
                self._bump("n_bad")
                return

        if b.kind == "folder":
            book_id, n_img, feats, blanks, thumb, err = self._scan_folder(b)
        else:
            book_id, n_img, feats, blanks, thumb, err = self._scan_archive(b)

        if err:
            self.errors.append((key, err))
            self._say(f"跳过（{b.path.name}）：{err}", "warn")
            self.cache.put_book(key, b.kind, b.fmt, b.size, b.mtime, n_img, 0, 0,
                                s.crop_mode, thumb=thumb, status="error", error=err[:300])
            self._bump("n_bad")
            return

        if book_id is None:
            book_id = self.cache.put_book(key, b.kind, b.fmt, b.size, b.mtime, n_img,
                                          len(feats), blanks, s.crop_mode, thumb=thumb,
                                          status="empty" if not feats else "ok")
        self._bump("n_scanned")
        self._bump("n_pages", len(feats))

    # ---------------- 压缩包
    def _scan_archive(self, b: A.BookEntry):
        s = self.s
        thumb = ""
        try:
            bk = A.open_book(b.path)
        except A.ArchiveError as e:
            return None, 0, [], 0, "", str(e)
        try:
            try:
                members = bk.list_images()
            except Exception as e:
                return None, 0, [], 0, "", f"列目录失败：{e}"
            n_img = len(members)
            if n_img == 0:
                return None, 0, [], 0, "", "压缩包内没有图片"
            names = [m[0] for m in members]
            want = sample_indices(n_img, s.anchors, s.window, s.max_pages)

            # 封面缩略图用第 0 张（不参与比对，只给人看）
            if s.make_thumbs:
                try:
                    first = bk.read(names[0])
                    thumb = self._save_thumb(b.path, first)
                except Exception:
                    thumb = ""

            data, read_note = self._read_members(bk, names, want)
            feats, blanks, failed, no_bytes = [], 0, 0, 0
            for idx in want:
                nm = names[idx]
                buf = data.get(nm)
                if not buf:
                    failed += 1
                    no_bytes += 1
                    continue
                f = core.page_feature(buf, idx, do_deskew=s.do_deskew, crop_mode=s.crop_mode)
                if f is None:
                    failed += 1
                    continue
                feats.append(f)
                blanks += 1 if f.blank else 0
            book_id = self.cache.put_book(str(b.path), b.kind, b.fmt, b.size, b.mtime,
                                          n_img, len(feats), blanks, s.crop_mode, thumb=thumb)
            self.cache.put_pages(book_id, feats)
            if failed:
                self._say(f"{b.path.name}：{failed} 张读取/解码失败已跳过", "warn")
            if not feats:
                # ⚠ 区分「抽取阶段就没拿到字节」和「拿到了但图片解不开」—— 以前两种情况
                # 都报「抽样页全部解码失败」，排查时看不出根因。真实案例：某 7z 的成员名
                # 被按错编码解成乱码，坏名字再拿去解压必然抽不到，却报成了"解码失败"。
                if want and no_bytes == len(want):
                    why = (f"抽样 {len(want)} 页全部抽取失败（压缩包成员本来就读不出来；"
                           f"常见原因：成员名编码不兼容、加密、数据损坏）")
                    return book_id, n_img, [], blanks, thumb, why + (f"｜{read_note}" if read_note else "")
                return book_id, n_img, [], blanks, thumb, \
                    f"抽样 {len(want)} 页全部解码失败（{failed} 张读到了但解不开）"
            return book_id, n_img, feats, blanks, thumb, ""
        finally:
            try:
                bk.close()
            except Exception:
                pass

    def _read_members(self, bk, names: Sequence[str], want: Sequence[int]) -> tuple:
        """把要抽的页一次读进内存。

        返回 ``(名字 -> bytes|None, 失败说明)``。第二个返回值专门用来把
        「抽取阶段就没拿到字节」和「拿到了但图片解不开」区分开 —— 调用方靠它给出
        可诊断的失败原因，而不是笼统的"解码失败"。

        外部解压程序一次调用抽完（而不是每张一次），zip/rarfile 走随机访问。
        """
        targets = [names[i] for i in want]
        out: dict = {}
        note = ""
        if isinstance(bk, A.ZipBackend) or isinstance(bk, A._RarfileBackend):
            for n in targets:
                try:
                    out[n] = bk.read(n)
                except Exception as e:
                    out[n] = None
                    note = note or f"{type(e).__name__}: {e}"
        else:
            try:
                got = bk.read_many(targets)
            except Exception as e:
                got = {}
                note = note or f"批量抽取失败 {type(e).__name__}: {e}"
                self._say(f"批量抽取失败，改逐张读取：{e}", "warn")
            for n in targets:
                buf = got.get(n)
                if not buf:
                    try:
                        buf = bk.read(n)
                    except Exception as e:
                        buf = None
                        note = note or f"逐张抽取失败 {type(e).__name__}: {e}"
                out[n] = buf
        return out, note

    # ---------------- 文件夹
    def _scan_folder(self, b: A.BookEntry):
        s = self.s
        try:
            files = sorted([p for p in b.path.iterdir()
                            if p.is_file() and p.suffix.lower() in A.IMG_EXT],
                           key=lambda p: A._natural_key(p.name))
        except Exception as e:
            return None, 0, [], 0, "", f"目录读取失败：{e}"
        n_img = len(files)
        if n_img == 0:
            return None, 0, [], 0, "", "目录内没有图片"
        want = sample_indices(n_img, s.anchors, s.window, s.max_pages)
        thumb = ""
        if s.make_thumbs:
            try:
                thumb = self._save_thumb(b.path, files[0].read_bytes())
            except Exception:
                thumb = ""
        feats, blanks, failed = [], 0, 0
        for idx in want:
            try:
                buf = files[idx].read_bytes()
            except Exception:
                failed += 1
                continue
            f = core.page_feature(buf, idx, do_deskew=s.do_deskew, crop_mode=s.crop_mode)
            if f is None:
                failed += 1
                continue
            feats.append(f)
            blanks += 1 if f.blank else 0
        book_id = self.cache.put_book(str(b.path), b.kind, b.fmt, b.size, b.mtime,
                                      n_img, len(feats), blanks, s.crop_mode, thumb=thumb)
        self.cache.put_pages(book_id, feats)
        if failed:
            self._say(f"{b.path.name}：{failed} 张图片解码失败已跳过", "warn")
        if not feats:
            return book_id, n_img, [], blanks, thumb, "抽样页全部解码失败"
        return book_id, n_img, feats, blanks, thumb, ""

    # ---------------- 缩略图
    def _save_thumb(self, src: Path, data: bytes) -> str:
        try:
            import hashlib
            from PIL import Image
            import io as _io
            im = Image.open(_io.BytesIO(data))
            im.draft("L", (400, 400))
            im = im.convert("L")
            h = 300
            w = max(1, int(im.width * h / max(1, im.height)))
            im = im.resize((w, h), Image.LANCZOS)
            key = hashlib.sha1(str(src).encode("utf-8", "replace")).hexdigest()[:20]
            out = self.thumb_dir / f"{key}.jpg"
            im.save(out, format="JPEG", quality=82)
            return out.name
        except Exception:
            return ""

    def thumb_path(self, name: str) -> Optional[Path]:
        if not name:
            return None
        p = self.thumb_dir / name
        return p if p.exists() else None


# ------------------------------------------------------------------ 分组


@dataclass
class Member:
    book_id: int = 0
    path: str = ""
    kind: str = ""
    fmt: str = ""
    size: int = 0
    pages: int = 0
    sampled: int = 0
    px: int = 0
    thumb: str = ""
    keep: bool = False
    score: float = 0.0
    matched: int = 0          # 成链页数（页序对得上的页数）
    ratio: float = 0.0        # 成链页数 / 较短那本的抽样页数
    pairs: list = field(default_factory=list)   # [(idx_a, idx_b, 分)]
    relation: str = ""        # 关系标签：完全相同 / 页码对齐·两版页数不同 / 有页码偏移 / 有页码偏移·两版页数不同


@dataclass
class Group:
    gid: int
    members: list = field(default_factory=list)
    score: float = 0.0
    matched: int = 0

    @property
    def total_size(self) -> int:
        return sum(m.size for m in self.members)

    @property
    def wasted(self) -> int:
        """删掉非保留项能省下的空间。"""
        return sum(m.size for m in self.members if not m.keep)


class UnionFind:
    def __init__(self):
        self.p: dict = {}

    def find(self, x):
        self.p.setdefault(x, x)
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]
            x = self.p[x]
        return x

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[rb] = ra
            return True
        return False

    def groups(self) -> dict:
        out: dict = {}
        for x in list(self.p):
            out.setdefault(self.find(x), []).append(x)
        return out


SUSPECT_WORDS = ("副本", "copy", "备份", "backup", "重打包", "重新打包", "低清", "压缩版",
                 "自制", "扫描", "扫图", "汉化组", "(1)", "(2)", "(3)", "[1]", "[2]",
                 "temp", "tmp", "old", "旧", "未整理")


def _name_suspects(name: str) -> int:
    """文件名里出现多少「看起来像副本/劣化」的字样。"""
    low = name.lower()
    return sum(1 for w in SUSPECT_WORDS if w.lower() in low)


def _book_quality(ms: Sequence[Member]) -> Member:
    """在一组重复书里挑出「建议保留」的那一本。**这只是建议，界面上随时可改。**

    判据依次是：

    1. **文件名里的可疑字样越少越好** —— 名字带「副本 / 重打包 / 备份 / 低清 /
       自制 / 扫描 / 汉化组」的版本，通常不是你想留的那一份。这是最直观、也最贴近
       用户意图的信号，所以放在第一位。
    2. **分辨率**（原图像素数），但**只在组内差距明显时才算数**（≥ 组内最高的 85%）。
       为什么加这个容差：自制扫描版的像素数往往**略高于**官方 DL 版（扫描仪按
       300dpi 出图，DL 版常见 1000~1400px 宽），可扫描版画质其实更脏（噪点、
       色偏、歪斜）。如果让分辨率无门槛地压过一切，就会出现
       「建议删官方 DL 版、留自制扫描版」这种反直觉结论 —— 实测踩过。
       两版像素相当（±15% 以内）时就不看它，交给下一条。
    3. 路径更短、文件体积更大。
    4. 路径字典序 —— 纯粹为了让**结果确定**：没有末级裁决时 max() 取到哪一个
       取决于输入顺序，同一批文件跑两次可能给出不同的建议（视频查重项目踩过这个坑）。
    """
    px_max = max((int(m.px) for m in ms), default=0)
    px_floor = px_max * 0.85

    def key(m: Member) -> tuple:
        p = str(m.path)
        px_tier = 1 if (px_max <= 0 or int(m.px) >= px_floor) else 0
        return (-_name_suspects(Path(p).name), px_tier, -len(p), int(m.size), p.lower())

    return max(ms, key=key)


def _quality_key(m: Member) -> tuple:
    """单本「画质」排序键（不含「组内相对分辨率」那一层，那个需要组上下文）。

    给并查集的**组代表互验**用：合并两组时要选一个代表，这里只看文件名干净度、
    路径长度、体积 —— 都是不依赖组内比较的量。
    """
    p = str(m.path)
    return (-_name_suspects(Path(p).name), -len(p), int(m.size), p.lower())


def compute_matches(A_tiles: np.ndarray, B_tiles: np.ndarray,
                    A_ph: np.ndarray, B_ph: np.ndarray,
                    page_thr: float) -> np.ndarray:
    """页对分数矩阵（平移精比 + pHash 融合）。

    A_tiles (m,64,64) / B_tiles (n,64,64) uint8；返回 (m,n) float32。

    性能：pHash 融合用**向量化的 popcount**（``core.popcount64``）一次算完整张
    汉明距离表，而不是逐页调用 Python 的 ``bin().count()``；图块先整体转 float32，
    避免 ``aligned_similarity`` 在热路径上重复 astype。
    """
    m, n = A_tiles.shape[0], B_tiles.shape[0]
    if m == 0 or n == 0:
        return np.zeros((m, n), dtype=np.float32)
    out = np.zeros((m, n), dtype=np.float32)
    A = A_tiles.astype(np.float32)
    B = B_tiles.astype(np.float32)
    # 预计算 pHash 汉明距离表（向量化）
    x = np.bitwise_xor(A_ph[:, None], B_ph[None, :])
    ham = core.popcount64(x.astype(np.uint64)).astype(np.float32)
    for i in range(m):
        for j in range(n):
            al = core.aligned_similarity(A[i], B[j])
            if al > 0.05:
                out[i, j] = core.fused_page_score(al, core.sim_from_hamming(float(ham[i, j]), 32))
            else:
                out[i, j] = 0.0
    return out


def _greedy_match(M: np.ndarray, thr: float) -> list:
    """贪心一对一匹配：按分数从高到低占位，避免同一页被重复计数。"""
    if M.size == 0:
        return []
    idx = np.argwhere(M >= thr)
    if idx.size == 0:
        return []
    vals = M[idx[:, 0], idx[:, 1]]
    order = np.argsort(-vals)
    used_a, used_b, res = set(), set(), []
    for k in order:
        i, j = int(idx[k, 0]), int(idx[k, 1])
        if i in used_a or j in used_b:
            continue
        used_a.add(i)
        used_b.add(j)
        res.append((i, j, float(M[i, j])))
    return res


def _lis_chain(ms: Sequence) -> int:
    """匹配页对里**最长递增链**的长度（页序一致性判据）。

    把匹配对按 A 的页序排好，对 B 的页序求最长严格递增子序列。
    同一本书的两个版本页序必然一致，所以真重复的匹配对会连成一条斜线
    （链长 ≈ 匹配页数）；而「版式雷同凑出来的假匹配」页码乱跳，链长很小。

    为什么不用「偏移量的 90 分位」那套（旧实现，已废弃）：
    匹配对数常常只有十几个，90 分位落在倒数第二个点上，**两个离群点就能把整对否掉**。
    实测 `某本书` 的两个版本有 11 页相似度 1.00（完全相同的图），
    只因多出来的两页造成两个离群偏移，就被旧判据判成「页序不自洽」而漏掉。
    LIS 天然容忍少数离群，只统计「成链」的部分。
    """
    if not ms:
        return 0
    ps = sorted((int(i), int(j)) for (i, j, _s) in ms)
    tails: list = []
    for _i, j in ps:
        k = bisect_left(tails, j)
        if k == len(tails):
            tails.append(j)
        else:
            tails[k] = j
    return len(tails)


def _relation(ms: Sequence, iax: Sequence[int], ibx: Sequence[int],
              pages_a: int, pages_b: int) -> str:
    """给一对重复书打一个人类可读的关系标签（思路来自 ComicDup 的「结果分类」）。

    标签按**机制**命名（v1.1.1）：旧版「页序相同 vs 页序一致」从字面无法分辨，
    实际区别是「匹配对的页号零偏移（同一物理版式）」vs「页号整体错开
    （一版多了广告页/版权页，顺序仍一致）」。

    注意：这是在**抽样页**上判的，给不出「具体缺哪几页」——那需要对两本做
    全量逐页比对（ComicDup 的按需全量比对），留待后续版本。
    """
    if not ms:
        return ""
    zero_off = all(int(iax[i]) == int(ibx[j]) for (i, j, _s) in ms)
    if zero_off and pages_a == pages_b:
        return "完全相同"
    if zero_off:
        return "页码对齐·两版页数不同"
    if pages_a == pages_b:
        return "有页码偏移"
    return "有页码偏移·两版页数不同"


@dataclass
class GroupSettings:
    page_thr: float = 0.62
    """页级相似度阈值（作用在 64×64 页图块的平移不变精比上）。"""
    min_pages: int = 3
    """至少有多少页**成链**（页序对得上）才算同一本。"""
    ratio_thr: float = 0.25
    """成链页数 / 较短那本的抽样页数。"""
    in_order: float = 0.6
    """成链页数 / 命中页数 —— 一对一匹配里至少这个比例要落在链上。

    只靠「命中多少页」不够：两版各自按块采样时，块内会有小幅乱序配对
    （A33↔B35、A34↔B33 …），还夹杂少量完全错位的离群点。真正的重复绝大多数
    匹配都在链上（实测真重复这个比值 ≥ 0.85），靠它把纯噪声对挤出去。
    """
    coarse_thr: float = 0.70
    """候选预筛的页级阈值（作用在 16×16 粗筛图上）。

    为什么是 0.70：实测 3067 本真实单行本，随机两页的 16×16 余弦有 4.4% 能过 0.55，
    一本 35 页的书就是几十个假命中；提到 0.70 后随机误入降到 0.5%，
    而真正对应的页对（含最难的"扫描版 vs 官方 DL 版"）仍能配上好几页。
    """
    pre_min: int = 5
    """预筛：两本书的匹配对里，命中页数**和**成链页数都要达标（绝对值或比例）才送进精比。

    双门槛（命中数 + LIS）是实测校准出来的：粗筛分数 0.70 下，只查「一对一命中 ≥5」
    会放进 1083 对，其中夹杂大量「共享版权页/广告页」的噪声；同时查「命中 ≥5 且
    成链 ≥5」只剩 701 对，几乎全是真重复。LIS 负责把页码乱跳的公共页挡在精比外。
    """
    pre_ratio: float = 0.2
    """预筛的比例兜底：命中数 / 成链数不足 ``pre_min`` 时，占较短那本书抽样页数的
    比例 ≥ 本值也送进精比（照顾只有十几页的短篇）。**绝对 OR 比例**，不是「且」。"""
    chain_floor: int = 8
    """精比比例门槛之外的**绝对链长地板**：成链页数 ≥ 此值即视为比例达标（0=关闭）。

    为什么需要：ratio = 成链页数 / 较短那本抽样页数。标准档每本抽 52 页时，
    ratio ≥ 0.25 隐式要求成链 ≥ 13 —— 比 LIS 分档表（真实库 1219 候选实测）给出的
    强分界 **LIS≥8** 还严，会把 8~12 档的真重复（实测真率 41.7%）整批切掉。
    LIS 本身就是强判别器（真重复 LIS 中位 28、噪声对中位 0），链长足够时
    不应再受比例门槛二次挤压。
    """
    threads: int = 0


def _page_desc(maps: np.ndarray) -> np.ndarray:
    """把每页的 16×16 粗筛图变成去均值单位向量（float32）。

    **为什么仍然用 16×16（而不是更锐的 32×32 / 64×64）**：这一步是为了**召回**，
    不是判别。实测最难的那一类（自制扫描版 vs 官方 DL 版）在 16×16 上仍能一一配上
    5~6 页，换成 32×32 只剩 1~2 页 —— 更锐的描述子对两版之间的**残余位移/缩放差**
    更敏感，反而把真重复漏掉。判别力不靠描述子，靠下一步的「一对一匹配页数」。

    顺带：``map16`` 是页特征里现成的 256 字节，不用解压 64×64 图块，省一次全库解压。
    """
    P = np.asarray(maps, dtype=np.float32)
    P = P - P.mean(axis=1, keepdims=True)
    nrm = np.sqrt((P * P).sum(axis=1, keepdims=True))
    nrm[nrm < 1e-3] = np.inf
    P /= nrm
    return P


def group_books(cache: FeatureCache, settings: GroupSettings,
                on_progress: Optional[Callable] = None,
                should_stop: Optional[Callable] = None) -> tuple:
    """执行比对分组。返回 (groups, pairs, stats)。"""
    t0 = time.time()
    rows = cache.all_pages(need_tile=False)
    if not rows:
        return [], [], {"msg": "缓存里没有页特征"}

    meta = {b["id"]: b for b in cache.book_meta()}
    # 预先把每本的分辨率聚合好：直接在循环里扫 rows 会退化到 O(书数 × 页数)
    px_by_book: dict = {}
    for r in rows:
        px_by_book.setdefault(int(r["bid"]), []).append(int(r["px"] or 0))

    # ---- 按书分组
    bids = np.array([r["bid"] for r in rows], dtype=np.int64)
    order = np.argsort(bids, kind="stable")
    bids = bids[order]
    maps = np.stack([np.frombuffer(rows[i]["map16"], dtype=np.uint8) for i in order])
    uniq, starts, counts = np.unique(bids, return_index=True, return_counts=True)
    nb = len(uniq)
    log.info("分组：%d 本书 / %d 页", nb, len(bids))

    # ---- 页描述子（16x16 去均值单位向量）
    P = _page_desc(maps)

    # ---- 候选书对：逐书矩阵乘法 + 一对一匹配 + **成链页数（LIS）**
    # 判据分两步：① 一对一贪心匹配得到「哪些页对最像」；② 看这些匹配对能连成
    # 多长的递增链（页序一致）。漫画整套书里版权页/广告页/预告页反复出现，
    # 两本不相干的书也会共享 2~4 页完全一样的图 —— 但那些匹配对页码乱跳、
    # 连不成链；只有「同一本书的两个版本」才会连成一条斜线。
    # 实测（3067 本真实单行本）：旧的「一对一命中页数」筛出 1219 对候选、
    # 精比准确率仅 21.7%；换成「成链页数」后，把真正的重复几乎全圈进来了，
    # 候选总量还更小，省下的时间花在精比上。
    cand: set = set()
    done = 0
    for k in range(nb):
        if should_stop and should_stop():
            break
        a0, a1 = int(starts[k]), int(starts[k] + counts[k])
        c0 = int(starts[k + 1]) if k + 1 < nb else len(bids)
        if c0 >= len(bids):
            continue
        S = P[a0:a1] @ P[c0:].T                      # (m, 后面的全部页)
        seg_start = starts[k + 1:] - c0
        seg_len = counts[k + 1:]
        spans = np.minimum(counts[k], seg_len)       # 每个候选书对里，抽样页数较少的那本
        # 先粗过一遍：raw 命中页对数（一对一匹配只会更少）连「绝对 / 比例」门槛的
        # 低者都不到的书对，直接跳过，省掉一对一匹配的功夫
        rough = np.add.reduceat((S >= settings.coarse_thr).sum(axis=0), seg_start)[: len(seg_len)]
        for t in np.nonzero((rough >= settings.pre_min) |
                            (rough >= settings.pre_ratio * spans))[0]:
            j0 = int(seg_start[t])
            j1 = j0 + int(seg_len[t])
            span = int(spans[t])
            ms = _greedy_match(S[:, j0:j1], settings.coarse_thr)
            n_match = len(ms)
            chain = _lis_chain(ms)
            # 双门槛（命中数 + 成链数），各自「绝对达标 **或** 占较短书抽样页数达标」。
            # 绝对门槛拦大部头之间的噪声；比例门槛放行「全书就十几页」的短篇
            # （v1.1.1 修复：旧代码第三道 if 里 n_match < pre_min 恒为 False——
            #   能走到那一步的必然已过绝对门槛——比例兜底从未生效过，短篇召回受损）。
            # （实测：粗筛分数 0.70 下，nm≥5 & LIS≥5 只剩 701 对，几乎全是真重复；
            #   只查 nm≥5 不查 LIS 会放进 1083 对，其中夹杂大量共享公共页的噪声。）
            ok_m = n_match >= settings.pre_min or n_match >= settings.pre_ratio * span
            ok_c = chain >= settings.pre_min or chain >= settings.pre_ratio * span
            if not (ok_m and ok_c):
                continue
            cand.add((int(uniq[k]), int(uniq[k + 1 + t])))
        done += 1
        if on_progress and (done % 20 == 0 or done == nb):
            on_progress(f"候选筛选 {done}/{nb}，已得候选书对 {len(cand)}")

    cand = sorted(cand)
    log.info("候选书对 %d 对", len(cand))

    # ---- 精比（多线程：matchTemplate 会释放 GIL）
    pairs_out: list = []
    thr = settings.page_thr
    nthreads = settings.threads or max(1, min(8, (os.cpu_count() or 4)))
    lock = threading.Lock()
    tiles_cache: dict = {}
    tiles_fifo: deque = deque()          # 简单 FIFO 淘汰
    tiles_max = 500                      # 约 500 本 × 35 页的图块 ≈ 70MB，够覆盖一轮精比

    def tiles_of(bid: int, need_tile: bool = True):
        with lock:
            t = tiles_cache.get(bid)
            if t is not None:
                return t
        d = cache.tiles_for_books([bid]).get(bid, [])
        with lock:
            if bid not in tiles_cache:
                tiles_cache[bid] = d
                tiles_fifo.append(bid)
                # 旧实现是「超过 40 本就整体 clear()」——候选对上万时会反复丢掉刚用过的书，
                # 每次都重新查库 + zlib 解压同一批图块。改成有上限的 FIFO 淘汰。
                while len(tiles_fifo) > tiles_max:
                    tiles_cache.pop(tiles_fifo.popleft(), None)
        return d

    def work(pair):
        ia, ib = pair
        if should_stop and should_stop():
            return None
        da, db = tiles_of(ia), tiles_of(ib)
        if not da or not db:
            return None
        TA = np.stack([np.frombuffer(t, np.uint8).reshape(core.TILE, core.TILE)
                       for (_i, t, _m, _p, _x) in da])
        TB = np.stack([np.frombuffer(t, np.uint8).reshape(core.TILE, core.TILE)
                       for (_i, t, _m, _p, _x) in db])
        PA = np.array([p for (_i, _t, _m, p, _x) in da], np.uint64)
        PB = np.array([p for (_i, _t, _m, p, _x) in db], np.uint64)
        M = compute_matches(TA, TB, PA, PB, thr)
        ms = _greedy_match(M, thr)
        if len(ms) < settings.min_pages:
            return None
        chain = _lis_chain(ms)
        if chain < settings.min_pages:
            return None
        # 成链页数占较短那本抽样页数的比例 —— 拦「只共享几页公共页」的假重复。
        # 比例不够但链长 ≥ chain_floor 的放行：LIS≥8 本身已是强证据（见 GroupSettings），
        # 不应被「抽样页数多导致的隐式比例地板」切掉（v1.1.1）。chain_floor=0 关闭此豁免。
        ratio = chain / float(max(1, min(len(da), len(db))))
        floor = settings.chain_floor
        if ratio < settings.ratio_thr and (floor <= 0 or chain < floor):
            return None
        # 成链页数占命中页数的比例 —— 拦「页序乱跳」的噪声对
        if chain < settings.in_order * len(ms):
            return None
        pairs = [(int(da[i][0]), int(db[j][0]), round(s, 4)) for (i, j, s) in ms]
        rel = _relation(ms, [x[0] for x in da], [x[0] for x in db],
                        int(meta.get(ia, {}).get("pages", 0)),
                        int(meta.get(ib, {}).get("pages", 0)))
        score = float(np.mean([s for (_i, _j, s) in ms]))
        return (ia, ib, chain, len(ms), ratio, score, rel, pairs)

    n_done = 0
    with ThreadPoolExecutor(max_workers=nthreads) as ex:
        futs = {ex.submit(work, p): p for p in cand}
        for fu in as_completed(futs):
            n_done += 1
            try:
                r = fu.result()
            except Exception as e:
                log.warning("精比异常 %s: %s", futs[fu], e)
                r = None
            if r:
                with lock:
                    pairs_out.append(r)
            if on_progress and (n_done % 50 == 0 or n_done == len(cand)):
                on_progress(f"精比 {n_done}/{len(cand)}，判重书对 {len(pairs_out)}")

    # ---- 并查集 + 组代表互验
    pairs_out.sort(key=lambda t: -t[5])
    by_book: dict = {}
    for (ia, ib, chain, nm, ratio, score, rel, pr) in pairs_out:
        by_book.setdefault(ia, []).append((ib, chain, nm, ratio, score, rel, pr))
        by_book.setdefault(ib, []).append((ia, chain, nm, ratio, score, rel, pr))
    uf = UnionFind()
    for b in uniq:
        uf.find(int(b))
    best_rep: dict = {}     # 组根 -> 组的「代表」书 id（画质最好的那本）

    def representative(bid):
        root = uf.find(bid)
        if root not in best_rep:
            best_rep[root] = bid
        return best_rep[root]

    for (ia, ib, chain, nm, ratio, score, rel, pr) in pairs_out:
        ra, rb = uf.find(ia), uf.find(ib)
        if ra == rb:
            continue
        # 组代表互验：两个组的代表之间也必须互相判重，才允许并组。
        # 否则 A~B、B~C 的传递性会把「同系列不同卷」（只共享封面/预告页）串成一大组。
        if ra in best_rep and rb in best_rep:
            pa, pb = best_rep[ra], best_rep[rb]
            if (pa, pb) != (ia, ib) and not _pair_ok(by_book, pa, pb, settings):
                log.info("阻止连环并组：%s ~ %s", Path(meta.get(ra, {}).get('path', '')).name,
                         Path(meta.get(rb, {}).get('path', '')).name)
                continue
        if uf.union(ia, ib):
            merged = [b for b in (best_rep.pop(ra, ia), best_rep.pop(rb, ib))]
            best_rep[uf.find(ia)] = max(merged, key=lambda b: _quality_key(
                Member(book_id=b, size=meta.get(b, {}).get("size", 0),
                       path=meta.get(b, {}).get("path", ""))))

    # ---- 组装结果
    groups: list = []
    for gi, (root, members) in enumerate(sorted(uf.groups().items()), 1):
        if len(members) < 2:
            continue
        ms = []
        for bid in members:
            bm = meta.get(bid, {})
            m = Member(book_id=bid, path=bm.get("path", ""), kind=bm.get("kind", ""),
                       fmt=bm.get("fmt", ""), size=int(bm.get("size", 0)),
                       pages=int(bm.get("pages", 0)), sampled=int(bm.get("sampled", 0)),
                       thumb=bm.get("thumb", "") or "")
            pxs = px_by_book.get(bid) or []
            m.px = int(np.median(pxs)) if pxs else 0
            rel = by_book.get(bid, [])
            rel = [x for x in rel if uf.find(x[0]) == uf.find(bid)]
            if rel:
                best = max(rel, key=lambda x: x[4])   # 按 score 取最可信的一条关系
                m.score, m.matched, m.ratio = best[4], best[1], best[3]
                m.pairs = best[6]
                m.relation = best[5]
                m.matched = max([x[1] for x in rel])
                m.score = max([x[4] for x in rel])
                m.ratio = max([x[3] for x in rel])
            ms.append(m)
        ms.sort(key=lambda x: (-x.score, x.path.lower()))
        keep = _book_quality(ms)
        for m in ms:
            m.keep = (m is keep)
        groups.append(Group(gid=gi, members=ms,
                            score=max(m.score for m in ms),
                            matched=max(m.matched for m in ms)))
    groups.sort(key=lambda g: (-g.wasted, -g.score))
    # 组号重新编成 1..N：内部用的是书号（会跳成 13/14/15 这种），
    # 对用户来说「组 1」才是可读的序号，而且排序后编号固定（可复现）。
    for i, g in enumerate(groups, 1):
        g.gid = i

    stats = {"books": nb, "pages": int(len(bids)), "candidates": len(cand),
             "pair_hits": len(pairs_out), "groups": len(groups),
             "elapsed": time.time() - t0, "dup_books": sum(len(g.members) for g in groups)}
    log.info("分组完成：%s", stats)
    return groups, pairs_out, stats


def _pair_ok(by_book: dict, a: int, b: int, settings: GroupSettings) -> bool:
    """查 a、b 之间是否已经算过并达标（成链页数 + 命中率）。"""
    for (x, chain, _nm, ratio, _score, _rel, _pr) in by_book.get(a, ()):
        if x == b and chain >= settings.min_pages and ratio >= settings.ratio_thr:
            return True
    for (x, chain, _nm, ratio, _score, _rel, _pr) in by_book.get(b, ()):
        if x == a and chain >= settings.min_pages and ratio >= settings.ratio_thr:
            return True
    return False


# ------------------------------------------------------------------ 导出


CSV_HEADER = ["组号", "建议保留", "单行本名称", "载体", "格式", "文件大小(字节)",
              "文件大小", "图片总页数", "抽样页数", "组内相似度", "成链页数", "命中率",
              "关系", "完整路径"]


def export_csv(groups: Sequence[Group], path: Path, root: Optional[Path] = None) -> int:
    """导出重复清单 CSV（含路径、页数、相似度）。返回写入行数。"""
    n = 0
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for g in groups:
            for m in g.members:
                rel = str(m.path)
                if root:
                    try:
                        rel = str(Path(m.path).relative_to(root))
                    except Exception:
                        rel = str(m.path)
                w.writerow([g.gid, "★保留" if m.keep else "", Path(m.path).name,
                            "文件夹" if m.kind == "folder" else "压缩包", m.fmt,
                            m.size, core.human_size(m.size), m.pages, m.sampled,
                            f"{m.score:.3f}", m.matched, f"{m.ratio:.2f}",
                            m.relation, str(m.path)])
                n += 1
    return n


def export_log(rows: Sequence[tuple], path: Path):
    """导出操作日志（删除记录等）。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["时间", "操作", "对象", "结果", "说明"])
        for r in rows:
            w.writerow(list(r))


# ------------------------------------------------------------------ 安全删除


def recycle_dir_for(root: Path) -> Path:
    """默认的本地回收站目录（与「删除」同卷，移动是常数时间）。"""
    return Path(root) / "_漫画查重回收站"


def delete_paths(paths: Sequence[str], recycle_dir: Optional[Path] = None,
                 use_trash: bool = True, on_log: Optional[Callable] = None,
                 max_items: int = 500) -> dict:
    """把选中的单行本移入回收站 / 本地回收目录。

    ⚠ 安全约束（有意为之，不要改）：

    * **绝不永久删除**。优先调用系统回收站（``send2trash``）；不可用时
      **移动到**本地回收目录（只移动，不销毁）。
    * 一次最多 **500 项**，避免误勾一大片后一口气搬走。
    * 每一项都**逐个执行并事后核验**（源路径是否已不存在），失败立刻停止，
      不会出现「报成功但实际没动」或「动到一半》的情况。
    * 全程写操作日志，可追溯。
    """
    res = {"moved": [], "failed": [], "log": []}
    paths = [str(p) for p in paths][:max_items]
    if not paths:
        return res

    trash_mod = None
    if use_trash:
        try:
            from send2trash import send2trash as _s2t
            trash_mod = _s2t
        except Exception:
            trash_mod = None
    rd = Path(recycle_dir) if recycle_dir else None
    if trash_mod is None and rd is None:
        rd = recycle_dir_for(Path(paths[0]).parent)
    if rd is not None:
        rd.mkdir(parents=True, exist_ok=True)

    for p in paths:
        src = Path(p)
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        if not src.exists():
            res["failed"].append((p, "源不存在"))
            res["log"].append((ts, "移动", p, "跳过", "源不存在"))
            continue
        ok, msg = False, ""
        if trash_mod is not None:
            try:
                trash_mod(str(src))
                ok = True
                msg = "已移入系统回收站"
            except Exception as e:
                msg = f"系统回收站失败（{type(e).__name__}），改用本地回收目录"
        if not ok:
            try:
                rd.mkdir(parents=True, exist_ok=True)
                dst = rd / src.name
                k = 1
                while dst.exists():
                    dst = rd / f"{src.stem}__{k}{src.suffix}"
                    k += 1
                shutil.move(str(src), str(dst))
                ok = True
                msg = f"已移动到 {dst}"
            except Exception as e:
                msg = f"移动失败：{type(e).__name__}: {e}"
        # 以「源是否还存在」为最终判据（个别环境回收站会回传错误码但实际已移走）
        gone = not src.exists()
        if ok and not gone:
            ok, msg = False, f"报告成功但源仍存在，按失败处理（{msg}）"
        elif not ok and gone:
            ok, msg = True, "源已不存在（回收站回传错误码，实际成功）"
        (res["moved"] if ok else res["failed"]).append((p, msg))
        res["log"].append((ts, "移动", p, "成功" if ok else "失败", msg))
        if on_log:
            try:
                on_log("info" if ok else "error", f"{'已移走' if ok else '未移走'} {p} —— {msg}")
            except Exception:
                pass
        if not ok:
            break        # 出错就停，绝不继续往下搬
    return res
