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
from dataclasses import dataclass, field
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
    # 名称: (锚点数, 窗口半宽, 每本最多抽多少页)
    "快速": (6, 0, 8),
    "标准": (8, 2, 40),
    "彻底": (14, 3, 100),
    "全页": (0, 0, 0),          # 0 = 所有页
}
PRESET_HELP = {
    "快速": "每本抽 8 页左右。适合先跑一遍找「同源改名/换格式」这类重复，最快。",
    "标准": "每本抽 40 页以内（8 个锚点各向两侧扩 2 页）。能覆盖「扫描版 vs 官方DL版」，推荐。",
    "彻底": "每本抽 100 页以内。两版页数差得多时更稳，但耗时长。",
    "全页": "每一页都算。最准最慢（3000 本 × 180 页 ≈ 54 万页，首次可能要好几个小时）。",
}


@dataclass
class ScanSettings:
    root: Path = Path(".")
    # --- 采样
    preset: str = "标准"
    anchors: int = 8
    window: int = 2
    max_pages: int = 40
    # --- 预处理
    crop_mode: str = core.DEFAULT_CROP
    do_deskew: bool = True
    # --- 缓存 / 缩略图
    use_cache: bool = True
    make_thumbs: bool = True
    # --- 判重
    page_thr: float = 0.62       # 页级相似度阈值
    min_pages: int = 3           # 至少多少页匹配才算同一本
    ratio_thr: float = 0.25      # 命中率下限（防同系列连环并组）
    coarse_thr: float = 0.55     # 粗筛阈值
    pre_min: int = 2             # 预筛：至少多少页对相似才进入精比
    # --- 其它
    max_books: int = 0           # 0 = 不限（调试用）
    threads: int = 0             # 0 = 自动

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

    策略：在整本书上均匀取 ``anchors`` 个锚点，每个锚点向两侧各扩 ``window`` 页。
    加窗口是为了容忍「两个版本页数不同」造成的对应页偏移 —— 两个版本各自按
    相对位置采到的页会差几页，靠窗口把对方真正的那一页也纳入进来，
    再由「整本多页集合匹配」在比对阶段把真正对应的页找出来。

    ``anchors=0`` 表示每一页都抽。跳过封面（第 0 页）—— 需求明确要求不要只比封面，
    而且第 0 页往往是别人的扫描封面，两版差异最大。
    """
    if page_count <= 0:
        return []
    if page_count <= 3:
        return list(range(page_count))
    if anchors <= 0:
        return list(range(page_count))
    body = page_count - 1
    idxs = set()
    if anchors == 1:
        centers = [1 + body // 2]
    else:
        centers = [1 + int(round(k * (body - 1) / (anchors - 1))) for k in range(anchors)]
    for c in centers:
        for d in range(-window, window + 1):
            j = c + d
            if 1 <= j < page_count:
                idxs.add(j)
    out = sorted(idxs)
    if max_pages and len(out) > max_pages:
        step = len(out) / float(max_pages)
        out = [out[min(len(out) - 1, int(i * step))] for i in range(max_pages)]
        out = sorted(set(out))
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
        self._n = threading.Semaphore(1)
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

    def _say(self, msg: str, level: str = "info"):
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

        for i, b in enumerate(books, 1):
            if self._stop.is_set():
                self.stats.skipped = total - i + 1
                self._say(f"已中断，剩余 {self.stats.skipped} 本未处理", "warn")
                break
            self._wait_if_paused()
            try:
                self._scan_one(b)
            except Exception as e:                      # 单本失败绝不影响全局
                self.stats.n_bad += 1
                self._record_error(b, f"{type(e).__name__}: {e}")
            if self.on_progress:
                try:
                    self.on_progress(i, total, str(b.path), self.stats)
                except Exception:
                    pass
        self.cache.flush()
        self.stats.elapsed = time.time() - t0
        self._say(f"扫描完成：{self.stats.n_books} 本，"
                  f"新算 {self.stats.n_scanned} 本 / 缓存命中 {self.stats.n_cached} 本，"
                  f"共 {self.stats.n_pages} 页特征，异常 {self.stats.n_bad} 本，"
                  f"耗时 {self.stats.elapsed:.1f} 秒")
        return self.stats

    # ---------------- 单本
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
                self.stats.n_cached += 1
                self.stats.n_pages += len(hit["pages"])
                return
            if hit and hit.get("status") == "error":
                # 上次就是坏的；文件没变就不再重试，避免每次扫描都卡同一个坏档
                self.stats.n_cached += 1
                self.stats.n_bad += 1
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
            self.stats.n_bad += 1
            return

        if book_id is None:
            book_id = self.cache.put_book(key, b.kind, b.fmt, b.size, b.mtime, n_img,
                                          len(feats), blanks, s.crop_mode, thumb=thumb,
                                          status="empty" if not feats else "ok")
        self.stats.n_scanned += 1
        self.stats.n_pages += len(feats)

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

            data = self._read_members(bk, names, want)
            feats, blanks, failed = [], 0, 0
            for idx in want:
                nm = names[idx]
                buf = data.get(nm)
                if not buf:
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
        finally:
            try:
                bk.close()
            except Exception:
                pass

    def _read_members(self, bk, names: Sequence[str], want: Sequence[int]) -> dict:
        """把要抽的页一次读进内存。

        外部解压程序一次调用抽完（而不是每张一次），zip/rarfile 走随机访问。
        """
        targets = [names[i] for i in want]
        out: dict = {}
        if isinstance(bk, A.ZipBackend) or isinstance(bk, A._RarfileBackend):
            for n in targets:
                try:
                    out[n] = bk.read(n)
                except Exception:
                    out[n] = None
        else:
            try:
                got = bk.read_many(targets)
            except Exception as e:
                self._say(f"批量抽取失败，改逐张读取：{e}", "warn")
                got = {}
            for n in targets:
                buf = got.get(n)
                if not buf:
                    try:
                        buf = bk.read(n)
                    except Exception:
                        buf = None
                out[n] = buf
        return out

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
    matched: int = 0
    ratio: float = 0.0
    pairs: list = field(default_factory=list)   # [(idx_a, idx_b, 分, 对齐分)]


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
    """
    m, n = A_tiles.shape[0], B_tiles.shape[0]
    if m == 0 or n == 0:
        return np.zeros((m, n), dtype=np.float32)
    out = np.zeros((m, n), dtype=np.float32)
    hi = max(page_thr + 0.15, 0.5)   # 先算对齐分，融合后可能被 pHash 抬高
    for i in range(m):
        for j in range(n):
            al = core.aligned_similarity(A_tiles[i], B_tiles[j])
            if al > 0.05:
                d = core.phash_hamming(int(A_ph[i]), int(B_ph[j]))
                out[i, j] = core.fused_page_score(al, core.sim_from_hamming(d, 32))
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


def _order_consistent(ms: list, idx_a: Sequence[int], idx_b: Sequence[int],
                      allow_viol: float = 0.25) -> bool:
    """检查匹配页对在**页序**上是否自洽。

    同一本书的两个版本，页序必然一致（漫画不可能乱页）。于是：

    * 把匹配对按 A 的页序排好后，B 的页序也应当基本单调递增；
    * 两版之间差的封面/插页会体现为一个**高度集中的常数偏移**（A页序 − B页序）。

    而「只是因为分镜版式雷同」凑出来的假匹配，页序是乱跳的 —— 这一步专门挡它。
    这正是扫描漫画指纹论文里「cut 序列」思路的推广：用页与页的**顺序关系**做判据，
    而不是只看单页像不像。
    """
    if len(ms) < 3:
        return False
    pairs = sorted((int(idx_a[i]), int(idx_b[j])) for (i, j, _s) in ms)
    bs = [b for _a, b in pairs]
    span = max(1, max(len(idx_a), len(idx_b)))
    tol = max(2, int(0.03 * span))
    viol = sum(1 for k in range(1, len(bs)) if bs[k] < bs[k - 1] - tol)
    if viol > allow_viol * (len(bs) - 1):
        return False
    offs = np.array([a - b for a, b in pairs], dtype=np.float32)
    med = float(np.median(offs))
    spread = float(np.percentile(np.abs(offs - med), 90))
    return spread <= max(8.0, 0.15 * span)


@dataclass
class GroupSettings:
    page_thr: float = 0.62
    min_pages: int = 3
    ratio_thr: float = 0.25
    coarse_thr: float = 0.55
    pre_min: int = 2
    threads: int = 0


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

    # ---- 按书分组，构造粗筛矩阵
    bids = np.array([r["bid"] for r in rows], dtype=np.int64)
    order = np.argsort(bids, kind="stable")
    bids = bids[order]
    maps = np.stack([np.frombuffer(rows[i]["map16"], dtype=np.uint8) for i in order])
    phs = np.array([int(rows[i]["phash"]) for i in order], dtype=np.uint64)
    bhs = np.array([int(rows[i]["bhash32"]) for i in order], dtype=np.uint32)
    uniq, starts, counts = np.unique(bids, return_index=True, return_counts=True)
    nb = len(uniq)
    log.info("分组：%d 本书 / %d 页", nb, len(bids))

    # 归一化的粗筛向量（一次算好，后面重复使用）
    P = maps.astype(np.float32)
    P -= P.mean(axis=1, keepdims=True)
    nrm = np.sqrt((P * P).sum(axis=1, keepdims=True))
    nrm[nrm < 1e-3] = np.inf
    P /= nrm

    # ---- 候选书对：逐书做一次矩阵乘法，按书汇总命中数
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
        hit = S >= settings.coarse_thr
        # 按书汇总：用 reduceat 在列方向分段求和，得到「这本书与每本后续书之间有多少个相似页对」。
        # ⚠ 判据是**页对总数**，不是「同一页命中对方多页」——
        # 同书两版是一一对应的，每个页在对方书里通常只命中 1 页（实测踩过这个坑）。
        seg_start = starts[k + 1:] - c0
        seg_len = counts[k + 1:]
        cnt = np.add.reduceat(hit.astype(np.int32), seg_start, axis=1)
        cnt = cnt[:, : len(seg_len)]
        totals = cnt.sum(axis=0)
        for t in np.nonzero(totals >= settings.pre_min)[0]:
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

    def tiles_of(bid: int, need_tile: bool = True):
        with lock:
            t = tiles_cache.get(bid)
            if t is not None:
                return t
        d = cache.tiles_for_books([bid]).get(bid, [])
        with lock:
            if len(tiles_cache) > 40:      # 简单 LRU：满了就整体清掉，避免占内存
                tiles_cache.clear()
            tiles_cache[bid] = d
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
        ratio = len(ms) / float(max(1, min(len(da), len(db))))
        if ratio < settings.ratio_thr:
            return None
        if not _order_consistent(ms, [x[0] for x in da], [x[0] for x in db]):
            return None
        pairs = [(int(da[i][0]), int(db[j][0]), round(s, 4)) for (i, j, s) in ms]
        return (ia, ib, len(ms), ratio, float(np.mean([s for (_i, _j, s) in ms])), pairs)

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
    pairs_out.sort(key=lambda t: -t[4])
    by_book: dict = {}
    for (ia, ib, cnt, ratio, score, pr) in pairs_out:
        by_book.setdefault(ia, []).append((ib, cnt, ratio, score, pr))
        by_book.setdefault(ib, []).append((ia, cnt, ratio, score, pr))
    uf = UnionFind()
    for b in uniq:
        uf.find(int(b))
    best_rep: dict = {}     # 组根 -> 组的「代表」书 id（画质最好的那本）

    def representative(bid):
        root = uf.find(bid)
        if root not in best_rep:
            best_rep[root] = bid
        return best_rep[root]

    for (ia, ib, cnt, ratio, score, pr) in pairs_out:
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
                best = max(rel, key=lambda x: x[3])
                m.score, m.matched, m.ratio = best[3], best[1], best[2]
                m.pairs = best[4]
                others = [x for x in rel if x[3] >= settings.page_thr]
                m.matched = max([x[1] for x in rel])
                m.score = max([x[3] for x in rel])
                m.ratio = max([x[2] for x in rel])
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
    """查 a、b 之间是否已经算过并达标。"""
    for (x, cnt, ratio, score, _pr) in by_book.get(a, ()):
        if x == b and cnt >= settings.min_pages and ratio >= settings.ratio_thr:
            return True
    for (x, cnt, ratio, score, _pr) in by_book.get(b, ()):
        if x == a and cnt >= settings.min_pages and ratio >= settings.ratio_thr:
            return True
    return False


# ------------------------------------------------------------------ 导出


CSV_HEADER = ["组号", "建议保留", "单行本名称", "载体", "格式", "文件大小(字节)",
              "文件大小", "图片总页数", "抽样页数", "组内相似度", "匹配页数", "命中率",
              "完整路径"]


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
                            f"{m.score:.3f}", m.matched, f"{m.ratio:.2f}", str(m.path)])
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
