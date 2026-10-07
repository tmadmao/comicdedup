"""图像预处理与感知特征提取（漫画查重的算法核心）。

为什么这样设计
--------------
漫画同一页存在两类版本：

* **自制扫描版**：纸张底色偏黄/偏灰、有装订阴影与光照渐变、带扫描仪黑边、
  可能轻微歪斜、有噪点与网纹（halftone）。
* **官方 DL 版**：干净、纯白底、无黑边、线条锐利。

要让两者匹配，必须把「与扫描设备相关」的信息全部剥掉，只留下「这一页画了什么」。
本模块的四步预处理正是干这个：

1. **自动裁剪**：只吃掉扫描仪盖板黑边（``DEFAULT_CROP="bed"``）—— **刻意不追求
   「裁得准」**。实测任何自适应阈值型的精细裁剪都会因为「扫描版噪声大、DL 版干净」
   而在两版上裁出不同的量，引入相对尺度差，反而让结果变差（详见 mass_box 的说明）。
2. **去斜（可选）**：用投影剖面峰值最大化估计倾斜角，旋转回正。默认开启但很便宜
   （在 256px 预览上搜索），角度很小或收益不明显时不动图。
3. **背景光照校正**：用大尺度背景估计做除法（document background division），
   消掉纸张底色与渐变阴影 —— 这是「全局亮度/对比度归一」做不到的部分。
4. **对比度归一化**：按 2%~98% 分位做线性拉伸，消掉色偏残留带来的对比度差异。

在此之上提取两个互补特征：

* **墨迹密度图（32x32，1024 维）**：INTER_AREA 降采样等于每格求平均，天然把
  网纹/噪点抹平；再用 3x3 高斯模糊，使特征对 ±1 格（±3% 幅面）的裁切误差不敏感。
  比对用 **皮尔逊相关系数（NCC）**，对残留的亮度/对比度差异免疫。
* **pHash（64 位）**：低频 DCT 符号化，抓整体版式与分镜结构，抗噪。

两路合并成页级相似度，再用「整本多页集合匹配」得到书级结论。
"""

from __future__ import annotations

import io
from dataclasses import dataclass
from typing import Optional

import cv2
import numpy as np
from PIL import Image

# ------------------------------------------------------------------ 常量

DENS_N = 32
"""墨迹密度图边长。32x32 = 1024 维，够抓分镜版式，又不至于被细节噪声干扰。"""

PHASH_LOW = 8
"""pHash 取 DCT 左上 8x8 低频块。"""

WORK_MAX = 1024
"""解码后的工作尺寸上限（长边），超过则先缩下来再处理。"""

CANON_MAX = 384
"""裁切/去斜之后归一到的最长边。所有重活都在这个尺度上做，保证速度。"""

SKEW_PREVIEW = 256
"""去斜估计用的预览长边。"""

SKEW_MAX_DEG = 5.0
"""去斜搜索角度上限（度）。扫描歪斜超过 5 度的情况很少，且大角度旋转代价高。"""

SKEW_MIN_GAIN = 1.12
"""去斜收益门槛：旋转后的投影尖锐度必须比不旋转高 12% 才动手，
避免在「本来就没歪」的页面上瞎转。比这个低就保持原样。"""

BLANK_STD = 12.0
"""空白页判定：归一化后像素标准差低于此值视为空白页（纯白/纯黑/近纯色）。"""

BLANK_INK = 0.004
"""空白页判定：墨迹像素占比低于 0.4% 也视为空白页。"""

MIN_CROP_KEEP = 0.30
"""自动裁剪的安全阀：单边裁掉超过 70% 就认为裁错了，放弃裁剪。"""

FEAT_VER = "c2"
"""特征算法版本。任何改动影响特征数值的修改都必须升级这个字符串，
否则旧缓存里的特征会被当成新版本误用。

c1 → c2：采样方式从「8 锚点 × ±2 页」改成「4 块 × 13 页」（见 engine.sample_indices）。
特征本身（页图块 / pHash / map16）没变，但**抽哪几页**变了，旧缓存里存的
是旧采样抽出来的页，必须失效重扫，否则块采样不生效。
"""


# ------------------------------------------------------------------ 小工具


def human_size(n: Optional[int]) -> str:
    """人类可读的字节数。"""
    if n is None:
        return "-"
    n = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return f"{n:.0f} {unit}" if unit == "B" else f"{n:.2f} {unit}"
        n /= 1024
    return f"{n:.2f} TB"


def _preview(gray: np.ndarray, long_side: int) -> np.ndarray:
    """等比缩到长边 = long_side（只缩不放）。"""
    h, w = gray.shape[:2]
    m = max(h, w)
    if m <= long_side:
        return gray
    s = long_side / float(m)
    return cv2.resize(gray, (max(1, int(round(w * s))), max(1, int(round(h * s)))),
                      interpolation=cv2.INTER_AREA)


# ------------------------------------------------------------------ 1. 解码


def image_size(data: bytes) -> tuple:
    """只读图片头拿原始尺寸（不解码像素，非常快）。失败返回 (0, 0)。"""
    try:
        with Image.open(io.BytesIO(data)) as im:
            return int(im.width), int(im.height)
    except Exception:
        return 0, 0


def decode_gray(data: bytes, work_max: int = WORK_MAX) -> Optional[np.ndarray]:
    """把图片字节解码成灰度 numpy 数组（uint8）。

    JPEG 走 PIL 的 draft 通道做**降采样解码**（DCT 缩放），大尺寸扫描页能快好几倍；
    其他格式正常解码后再等比缩小。解码失败返回 None（调用方记日志、跳过，不崩）。
    """
    try:
        im = Image.open(io.BytesIO(data))
    except Exception:
        return None
    try:
        # draft 必须在 load 之前调用；对不支持 draft 的格式会静默无效
        try:
            im.draft("L", (work_max, work_max))
        except Exception:
            pass
        try:
            im = im.convert("L")
        except Exception:
            return None
        arr = np.asarray(im, dtype=np.uint8)
    except Exception:
        return None
    finally:
        try:
            im.close()
        except Exception:
            pass
    if arr.ndim != 2 or arr.size == 0:
        return None
    return _preview(arr, work_max)


# ------------------------------------------------------------------ 2. 自动裁剪


LIGHT_MIN = 88
"""判定「纸面」的最低灰度（备用）。"""

INK_FLOOR = 0.006
"""墨迹包围盒的噪声地板：某一行/列至少要有 0.6% 的墨迹像素才算「有内容」，
低于它就当纸面。用来挡住扫描噪点和纸纹，避免包围盒被撑到页面边缘。"""

CROP_PREVIEW = 512
"""裁剪估计用的预览长边。"""

DEFAULT_CROP = "bed"
"""默认裁剪模式。**实测「只去扫描仪盖板」最优**，详见 auto_crop / aligned_similarity 注释。
可选 bed / mass / edge / paper（后三者保留供对比实验与调参）。"""

MAX_TRIM = 0.22
"""逐边最多裁掉 22%。扫描仪黑边通常只有几个百分点，这个上限足以覆盖，
又能防止「整幅宽的黑色分镜」被一路啃掉。"""

UNIFORM_STD = 9.0
"""「近似纯色」的标准差上限。"""

BRIGHT_BG = 180.0
"""纯色边若亮于此值，按纸面白边处理。"""

DARK_BG = 75.0
"""纯色边若暗于此值，按扫描仪盖板黑边处理。"""


def crop_box(gray: np.ndarray, mode: str = DEFAULT_CROP) -> tuple:
    """算出需要裁掉的外框 (top, bottom, left, right)。

    目标：让「扫描版」和「官方DL版」各自裁到**同一块画面内容**，这样两者才能逐格比对。

    ⚠ 这里走过 5 版弯路，结论写在这里免得重蹈覆辙：

    * ❌ 「只要近似纯色就算边距」—— 分镜框横线正好是「整行纯黑且均匀」，会被当边距裁掉，
      画面被切掉一圈。
    * ❌ 「最大亮连通块 = 纸面」—— 整幅宽的黑色分镜会把纸面切成两块，包围盒塌掉一半。
    * ❌ 「从画面中心泛洪找纸面」—— 中心那一块可能只是某个分镜格，同样会塌。
    * ❌ 用全局 Otsu 判墨迹 —— **扫描版光照渐变让同一行内亮度差 60+ 灰度**，全局阈值失效。
    * ✅ 最终方案见下面三个阶段，且**不再追求绝对精确**：残余的对齐误差交给
      `aligned_similarity()` 做平移搜索（实测这一步才是关键，见 probe_align）。

    三个阶段：
      1. **盖板黑边**：按行/列平均亮度吃掉明显偏暗的外围（扫描仪盖板）。
      2. **纸面留白**：按**高频边缘能量** hp = |gray - GaussianBlur(gray, σ≈短边/9)| 判定 ——
         光照渐变被低频吸收掉，纸面 hp≈0、画面 hp 高，判据与渐变彻底解耦。
         ⚠ 统计必须**区域受限**：整幅统计时每一行都穿过左右两条垂直黑边，
         黑边跳变把纸面行的能量抬到 20+（阈值 10.4），纸边就永远裁不掉（实测踩过）。
      3. **墨迹包围盒**：再做一次光照校正 + Otsu 取墨迹包围盒，把结果锚到画面本身。

    ``mode`` 只有三种取值会走到这个函数：``bed``（盖板黑边，**出厂默认**）、
    ``edge``（+ 纸面留白 + 墨迹包围盒）、``paper``（先按亮区纸面连通块定框）。
    后两者实测更差（见 auto_crop 的对照表），保留供对比实验与调参。
    """
    h, w = gray.shape[:2]
    pv = _preview(gray, CROP_PREVIEW)
    ph, pw = pv.shape[:2]
    if ph < 24 or pw < 24:
        return 0, h, 0, w
    pv = cv2.medianBlur(pv, 3)  # 压掉孤立噪点

    y0, y1, x0, x1 = 0, ph, 0, pw
    if mode == "paper":
        rect = _paper_rect(pv)
        if rect is not None:
            y0, y1, x0, x1 = rect
    if mode in ("edge", "bed", "paper"):
        # ---- 阶段 1：盖板黑边（按原始亮度；窄黑边必须用原始亮度，大尺度低频会把它抹平）
        for _ in range(2):
            y0, y1 = _trim_by(y0, y1, _level(pv, y0, y1, x0, x1, axis=1), DARK_BG, 0.15)
            x0, x1 = _trim_by(x0, x1, _level(pv, y0, y1, x0, x1, axis=0), DARK_BG, 0.15)
    if mode in ("edge", "paper"):
        # ---- 阶段 2：纸面留白（按高频边缘能量，区域受限）
        for _ in range(3):
            y0, y1 = _trim_by(y0, y1, _edge(pv, y0, y1, x0, x1, axis=1), None, MAX_TRIM)
            x0, x1 = _trim_by(x0, x1, _edge(pv, y0, y1, x0, x1, axis=0), None, MAX_TRIM)
        # ---- 阶段 3：墨迹包围盒（光照校正 + Otsu + 相对阈值）
        for _ in range(2):
            y0, y1 = _ink_trim(pv, y0, y1, x0, x1, axis=1, cap=MAX_TRIM)
            x0, x1 = _ink_trim(pv, y0, y1, x0, x1, axis=0, cap=MAX_TRIM)
        # ---- 阶段 4：再补一轮盖板（纸边裁掉后可能又露出过渡带）
        y0, y1 = _trim_by(y0, y1, _level(pv, y0, y1, x0, x1, axis=1), DARK_BG, 0.15)
        x0, x1 = _trim_by(x0, x1, _level(pv, y0, y1, x0, x1, axis=0), DARK_BG, 0.15)

    if y1 - y0 < ph * MIN_CROP_KEEP or x1 - x0 < pw * MIN_CROP_KEEP:
        return 0, h, 0, w

    sy, sx = h / float(ph), w / float(pw)
    top, bottom = max(0, int(round(y0 * sy))), min(h, int(round(y1 * sy)))
    left, right = max(0, int(round(x0 * sx))), min(w, int(round(x1 * sx)))
    if bottom - top < h * MIN_CROP_KEEP or right - left < w * MIN_CROP_KEEP:
        return 0, h, 0, w
    return top, bottom, left, right


def _paper_rect(pv: np.ndarray) -> Optional[tuple]:
    """找「纸面区域」的包围盒，用来把扫描仪盖板挡在统计之外。

    做法：
      1. Otsu 分亮区（纸）。扫描版盖板很暗、纸面偏亮（哪怕偏黄），一分为二很干净。
      2. 形态学闭运算把细框线桥接起来（盖板黑边远宽于框线，桥不过去）。
      3. **取所有「够大」的亮连通块的并集包围盒**。
         ⚠ 不能只取最大的一块：整幅宽的黑色分镜会把纸面切成上、下（或左、右）两块，
         取最大的一块会让包围盒塌掉一半 —— 同页相似度直接从 0.79 掉到 0.08（实测踩过）。
         也不能「从中心泛洪」：中心那一块可能只是某个分镜格，同样会塌。
         用并集 + 面积门槛（滤掉盖板上的噪点小块）两个问题一起解决。
    """
    h, w = pv.shape[:2]
    try:
        t, _ = cv2.threshold(pv, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    except Exception:
        t = 128.0
    t = max(float(t), float(LIGHT_MIN))
    light = (pv >= t).astype(np.uint8)
    if light.mean() < 0.05:      # 亮区太少（暗页/纯黑页），不信任这个估计
        return None
    k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (7, 7))
    closed = cv2.morphologyEx(light, cv2.MORPH_CLOSE, k)
    try:
        n, _labels, stats, _c = cv2.connectedComponentsWithStats(closed, 8)
    except Exception:
        return None
    if n <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA].astype(np.float64)
    keep = np.nonzero(areas >= max(0.02 * h * w, 0.15 * float(areas.max())))[0] + 1
    if keep.size == 0:
        return None
    left = int(stats[keep, cv2.CC_STAT_LEFT].min())
    top = int(stats[keep, cv2.CC_STAT_TOP].min())
    right = int((stats[keep, cv2.CC_STAT_LEFT] + stats[keep, cv2.CC_STAT_WIDTH]).max())
    bottom = int((stats[keep, cv2.CC_STAT_TOP] + stats[keep, cv2.CC_STAT_HEIGHT]).max())
    if (bottom - top) < h * 0.3 or (right - left) < w * 0.3:
        return None
    return top, bottom, left, right


def _ink_trim(pv: np.ndarray, y0: int, y1: int, x0: int, x1: int, axis: int,
              cap: float) -> tuple:
    """按「墨迹包围盒」吃掉边距 —— 本项目里最有效的一步。

    ⚠ 为什么不能直接对原图 Otsu：**扫描版的光照渐变会让同一行内亮度差 60+ 灰度**，
    全局阈值失效。所以先做一次**光照校正**（大尺度背景除法）把纸面压成均匀白，
    这时 Otsu 才切得准。

    阈值用**相对判据**：对逐行/列的墨迹占比剖面再做一次 Otsu，取「Otsu 分档」与
    「绝对噪声地板 INK_FLOOR」中较大的那个。因为纸面边距的占比通常 <1%、
    而画面行 3%~9%，相对分档能自适应地把两者分开，不像固定阈值那样在
    「纸面残留 2% 噪点」时全盘失效（实测踩过）。
    """
    sub = pv[y0:y1, x0:x1]
    h, w = sub.shape
    if h < 24 or w < 24:
        return (y0, y1) if axis == 1 else (x0, x1)

    # 光照校正：缩小 → 大尺度高斯（≈超大面积均值）→ 放大回原尺寸 → 相除
    sw, sh = max(4, w // 8), max(4, h // 8)
    small = cv2.resize(sub, (sw, sh), interpolation=cv2.INTER_AREA)
    bg = cv2.GaussianBlur(small, (0, 0), sigmaX=4.0)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    flat = np.clip(sub.astype(np.float32) * (255.0 / np.maximum(bg.astype(np.float32), 1.0)),
                   0, 255)

    try:
        t, _ = cv2.threshold(flat.astype(np.uint8), 0, 255,
                             cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    except Exception:
        t = 128.0
    ink = (flat < float(t)).astype(np.float32)
    if ink.mean() < 0.0005:          # 几乎找不到墨迹（暗页/纯色页），不动
        return (y0, y1) if axis == 1 else (x0, x1)

    prof = ink.mean(axis=axis)
    n = len(prof)
    lim = max(1, int(n * cap))
    thr = max(INK_FLOOR, _prof_thr(prof))
    ok = prof >= thr
    i = 0
    while i < lim and not ok[i]:
        i += 1
    j = n
    while j > n - lim and not ok[j - 1]:
        j -= 1
    if j - i < 16:
        return (y0, y1) if axis == 1 else (x0, x1)
    if axis == 1:
        return y0 + i, y0 + j
    return x0 + i, x0 + j


def _prof_thr(prof: np.ndarray) -> float:
    """对占比剖面做 Otsu 分档，返回「边距档」与「画面档」之间的分界。"""
    s = np.asarray(prof, dtype=np.float32)
    if s.size < 16:
        return 0.0
    lo, hi = float(s.min()), float(s.max())
    if hi - lo < 1e-6:
        return 0.0
    u8 = np.clip((s - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8).reshape(-1, 1)
    try:
        t, _ = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    except Exception:
        t = 128.0
    return lo + float(t) / 255.0 * (hi - lo)


def _level(pv: np.ndarray, y0: int, y1: int, x0: int, x1: int, axis: int) -> np.ndarray:
    """区域内的逐行/列平均亮度。"""
    return pv[y0:y1, x0:x1].astype(np.float32).mean(axis=axis)


def _edge(pv: np.ndarray, y0: int, y1: int, x0: int, x1: int, axis: int) -> np.ndarray:
    """区域内的逐行/列高频边缘能量（对光照渐变免疫）。"""
    sub = pv[y0:y1, x0:x1].astype(np.float32)
    if sub.size < 64:
        return np.zeros(sub.shape[axis], np.float32)
    sigma = max(4.0, min(sub.shape) / 18.0)
    hp = np.abs(sub - cv2.GaussianBlur(sub, (0, 0), sigmaX=sigma))
    return hp.mean(axis=axis)


def _trim_by(a: int, b: int, score: np.ndarray, thr: Optional[float],
             cap: float) -> tuple:
    """从 [a,b) 两端往里吃，吃掉连续「低于阈值」的行/列（最多各吃 cap 比例）。

    thr 传 None 表示用 _edge_thr 自适应求阈值。
    """
    n = min(b - a, len(score))
    if n < 24:
        return a, b
    s = np.asarray(score[:n], dtype=np.float32)
    t = _edge_thr(s) if thr is None else float(thr)
    lim = max(1, int(n * cap))
    ok = s < t
    i = 0
    while i < lim and ok[i]:
        i += 1
    j = n
    while j > n - lim and ok[j - 1]:
        j -= 1
    if j - i < 16:
        return a, b
    return a + i, a + j


def _edge_thr(score: np.ndarray) -> float:
    """边缘能量的自适应阈值。

    Otsu 在「纸面留白 vs 画面」两档之间切一刀；但如果整页都是画面（没有留白），
    Otsu 会把中间切一刀造成误裁，所以再叠加一个「不得高于内容能量 30%」的上限。
    """
    s = np.asarray(score, dtype=np.float32)
    if s.size < 8:
        return 0.0
    lo = float(np.percentile(s, 2))
    hi = float(np.percentile(s, 98))
    if hi - lo < 1e-3:
        return 0.0
    u8 = np.clip((s - lo) * (255.0 / (hi - lo)), 0, 255).astype(np.uint8).reshape(-1, 1)
    try:
        t, _ = cv2.threshold(u8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    except Exception:
        t = 128.0
    otsu = lo + float(t) / 255.0 * (hi - lo)
    return float(min(otsu, 0.30 * float(np.percentile(s, 90))))


def _trim_uniform(pv: np.ndarray, x0: int, y0: int, x1: int, y1: int) -> tuple:
    """保留备用：从外向内裁掉「近似纯色」的行列。

    ⚠ 注意：这个函数**不能**用在漫画页上 —— 分镜框的横线正好是「整行纯黑」，
    会被误判成纯色边而裁掉，导致画面被切掉一圈。目前只作为调试工具保留，
    主流程（crop_box）不使用它。
    """
    sub = pv[y0:y1, x0:x1]
    if sub.size == 0 or min(sub.shape) < 8:
        return x0, y0, x1, y1
    f = sub.astype(np.float32)
    rs, cs = f.std(axis=1), f.std(axis=0)
    h, w = sub.shape
    ty, by = int(h * 0.25), h - int(h * 0.25)
    lx, rx = int(w * 0.25), w - int(w * 0.25)
    t = 0
    while t < ty and rs[t] < 10.0:
        t += 1
    b = h
    while b > by and rs[b - 1] < 10.0:
        b -= 1
    l = 0
    while l < lx and cs[l] < 10.0:
        l += 1
    r = w
    while r > rx and cs[r - 1] < 10.0:
        r -= 1
    if b <= t or r <= l:
        return x0, y0, x1, y1
    return x0 + l, y0 + t, x0 + r, y0 + b


def mass_box(gray: np.ndarray, lo: float = 0.03, hi: float = 0.97) -> tuple:
    """用**墨迹质量的百分位**定出内容框 (top,bottom,left,right)。

    ⚠ **这个函数不在出厂流程里**（出厂是 ``DEFAULT_CROP="bed"``，只去盖板黑边）。
    它是「盖板 + 墨迹质量分位尺度归一」那条路线的实现，保留下来是为了让
    README「4.3 裁剪越准反而越差」那张对照表**可复现**（``--crop mass``）。

    思路本身是对的，方向却是错的：

    * 用「第一/最后一个非零行」这类极值判据，会被扫描噪点、纸纹、残留黑边带偏；
    * 用**分位数**（默认丢掉墨迹质量最外侧的 3%+3%）则对离群点天然免疫。

    但任何「自适应阈值型」的尺度归一，在**扫描版（噪声大）**和**官方 DL 版（干净）**
    上裁掉的量必然不一致，于是引入一个**相对尺度差**，而平移搜索修不了尺度差 ——
    于是同页中位相似度从 0.814（bed）掉到 0.482、异页最高反而升到 0.787，
    多页投票直接全失效（实测 24 页合成语料）。

    结论：**裁剪只做最低限度的去盖板黑边，剩下的对齐误差交给
    `aligned_similarity()` 的平移搜索**。
    """
    h, w = gray.shape[:2]
    pv = _preview(gray, CROP_PREVIEW)
    ph, pw = pv.shape[:2]
    if ph < 32 or pw < 32:
        return 0, h, 0, w
    pv = cv2.medianBlur(pv, 3)
    # 光照校正后 Otsu，判据才能不受纸张底色/渐变影响
    sw, sh = max(4, pw // 8), max(4, ph // 8)
    small = cv2.resize(pv, (sw, sh), interpolation=cv2.INTER_AREA)
    bg = cv2.GaussianBlur(small, (0, 0), sigmaX=4.0)
    bg = cv2.resize(bg, (pw, ph), interpolation=cv2.INTER_LINEAR)
    flat = np.clip(pv.astype(np.float32) * (255.0 / np.maximum(bg.astype(np.float32), 1.0)), 0, 255)
    try:
        t, _ = cv2.threshold(flat.astype(np.uint8), 0, 255,
                             cv2.THRESH_BINARY_INV + cv2.THRESH_OTSU)
    except Exception:
        t = 128.0
    ink = (flat < float(t)).astype(np.float32)
    if ink.sum() < 100:
        return 0, h, 0, w

    def span(prof: np.ndarray) -> tuple:
        c = np.cumsum(prof)
        if c[-1] <= 0:
            return 0, len(prof)
        c /= c[-1]
        a = int(np.searchsorted(c, lo))
        b = int(np.searchsorted(c, hi)) + 1
        return a, min(b, len(prof))

    ya, yb = span(ink.mean(axis=1))
    xa, xb = span(ink.mean(axis=0))
    if (yb - ya) < ph * 0.25 or (xb - xa) < pw * 0.25:
        return 0, h, 0, w
    sy, sx = h / float(ph), w / float(pw)
    top, bottom = max(0, int(round(ya * sy))), min(h, int(round(yb * sy)))
    left, right = max(0, int(round(xa * sx))), min(w, int(round(xb * sx)))
    if bottom - top < h * 0.25 or right - left < w * 0.25:
        return 0, h, 0, w
    return top, bottom, left, right


def auto_crop(gray: np.ndarray, mode: str = DEFAULT_CROP) -> np.ndarray:
    """按指定模式裁剪。

    * mode="bed"  —— 只吃掉扫描仪盖板黑边（**出厂默认，实测最优**：位置信息完整保留，
      两版裁掉的量一致，残余错位交给 aligned_similarity 的平移搜索）
    * mode="mass" —— 盖板黑边 + 墨迹质量分位尺度归一（实测很差：同页中位 0.48、
      异页最高 0.79，多页投票全失效。保留仅为复现那次失败实验）
    * mode="edge" —— 盖板 + 高频边缘能量逐边硬裁（同样是失败实验）
    * mode="paper"—— 盖板 + 亮区纸面连通块（同样是失败实验）

    后三者的实测对照见 README「四、4.3 最关键的一步：平移不变度量」。
    """
    h, w = gray.shape[:2]
    if mode == "mass":
        top, bottom, left, right = mass_box(gray)
    else:
        top, bottom, left, right = crop_box(gray, mode=mode)
    if (top, bottom, left, right) == (0, h, 0, w):
        return gray
    out = gray[top:bottom, left:right]
    return out if out.size else gray


# ------------------------------------------------------------------ 3. 去斜（可选）


def estimate_skew(gray: np.ndarray, max_deg: float = SKEW_MAX_DEG) -> float:
    """估计倾斜角（度）。正数表示需要逆时针旋转回来。

    目标函数：旋转后「墨迹行/列投影剖面」的二阶能量 —— 分镜框线与文字行对齐时
    投影最尖锐，能量最大。粗搜 1 度，再在最优附近细搜 0.2 度。
    """
    pv = _preview(gray, SKEW_PREVIEW)
    h, w = pv.shape[:2]
    if h < 24 or w < 24:
        return 0.0
    f = pv.astype(np.float32)
    ink = (f < 160).astype(np.float32)
    if ink.sum() < 50:
        return 0.0

    def score(angle: float) -> float:
        if abs(angle) < 1e-6:
            rot = ink
        else:
            m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
            rot = cv2.warpAffine(ink, m, (w, h), flags=cv2.INTER_NEAREST,
                                 borderMode=cv2.BORDER_CONSTANT, borderValue=0.0)
        rp = rot.mean(axis=1)
        cp = rot.mean(axis=0)
        return float(np.sum(np.diff(rp) ** 2) + np.sum(np.diff(cp) ** 2))

    base = score(0.0)
    if base <= 1e-9:
        return 0.0
    best_a, best_s = 0.0, base
    a = -max_deg
    while a <= max_deg + 1e-9:
        s = score(a)
        if s > best_s:
            best_a, best_s = a, s
        a += 1.0
    # 细搜
    a = best_a - 1.0
    while a <= best_a + 1.0 + 1e-9:
        s = score(a)
        if s > best_s:
            best_a, best_s = a, s
        a += 0.2
    if best_s < base * SKEW_MIN_GAIN or abs(best_a) < 0.3:
        return 0.0
    return float(max(-max_deg, min(max_deg, best_a)))


def rotate(gray: np.ndarray, angle_deg: float) -> np.ndarray:
    """按角度旋转（白底填充），并裁掉旋转产生的空白角。"""
    if abs(angle_deg) < 1e-6:
        return gray
    h, w = gray.shape[:2]
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle_deg, 1.0)
    cos, sin = abs(m[0, 0]), abs(m[0, 1])
    nw = int(h * sin + w * cos)
    nh = int(h * cos + w * sin)
    m[0, 2] += nw / 2.0 - w / 2.0
    m[1, 2] += nh / 2.0 - h / 2.0
    return cv2.warpAffine(gray, m, (nw, nh), flags=cv2.INTER_LINEAR,
                          borderMode=cv2.BORDER_REPLICATE)


def deskew(gray: np.ndarray) -> np.ndarray:
    """估计并纠正倾斜（收益不明显时原样返回）。"""
    a = estimate_skew(gray)
    if a == 0.0:
        return gray
    return rotate(gray, a)


# ------------------------------------------------------------------ 4. 归一化


def canon_size(gray: np.ndarray, long_side: int = CANON_MAX) -> np.ndarray:
    """缩放到统一尺寸（长边 = long_side，保持宽高比）。"""
    h, w = gray.shape[:2]
    m = max(h, w)
    if m == 0:
        return gray
    s = long_side / float(m)
    nw, nh = max(8, int(round(w * s))), max(8, int(round(h * s)))
    interp = cv2.INTER_AREA if s < 1.0 else cv2.INTER_LINEAR
    return cv2.resize(gray, (nw, nh), interpolation=interp)


def normalize(gray: np.ndarray) -> np.ndarray:
    """纸张底色/光照校正 + 对比度归一化。

    1. 背景估计：缩小 → 高斯模糊（相当于超大面积均值滤波）→ 放大回原尺寸。
       用除法把背景抹平，纸张偏黄、装订阴影、光照渐变都会被消掉，
       而墨迹是相对背景的高频结构，会被保留甚至增强。
    2. 分位拉伸：把 2%~98% 分位映射到 0~255，消掉残留的对比度差异。
    """
    h, w = gray.shape[:2]
    if h < 8 or w < 8:
        return gray
    sw, sh = max(4, w // 8), max(4, h // 8)
    small = cv2.resize(gray, (sw, sh), interpolation=cv2.INTER_AREA)
    bg = cv2.GaussianBlur(small, (0, 0), sigmaX=5.0)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    bg = np.maximum(bg.astype(np.float32), 1.0)
    flat = gray.astype(np.float32) * (255.0 / bg)
    np.clip(flat, 0, 255, out=flat)

    lo, hi = np.percentile(flat, (2.0, 98.0))
    if hi - lo < 25.0:  # 近乎纯色，拉伸没意义，直接收敛到区间中值附近
        return np.full_like(flat, 255.0 if flat.mean() > 127 else 0.0)
    out = (flat - lo) * (255.0 / (hi - lo))
    np.clip(out, 0, 255, out=out)
    return out


# ------------------------------------------------------------------ 特征

TILE = 64
"""页图块边长。归一化后的整页压成 TILE×TILE 灰度图，作为页级比对的底图。

实测：TILE=96 反而更差（对残余错位更敏感），64 是甜点。
"""

TPL = 48
"""模板边长。取页图块中心的 TPL×TPL 作为模板，在另一页上滑窗搜索，
搜索范围 ±(TILE-TPL)/2 = ±8 格 ≈ ±12.5% 幅面。"""

TILE_SMALL = 16
"""粗筛图边长。用于「先粗筛再精比」的两级级联，与 AntiDupl 的做法一致。"""


@dataclass
class PageFeat:
    """单页特征。

    为什么同时存三样东西：
      * ``tile``     —— 64×64 灰度页图块，精比用（平移搜索的底图）；
      * ``map16``    —— 16×16 粗筛图，粗筛用（模糊后对 ±12.5% 错位免疫）；
      * ``phash``/``bhash32`` —— 两个哈希通道，**候选筛选**用（比全量精比快几个数量级）。
    """

    idx: int          # 页序号（按压缩包内图片名自然排序后的 0 基下标）
    phash: int        # 64 位感知哈希（DCT，抓整体版式）
    bhash32: int      # 32 位块均值哈希（抓墨迹分布，与 pHash 互补）
    tile: bytes       # TILE*TILE uint8
    map16: bytes      # TILE_SMALL*TILE_SMALL uint8
    aspect: float     # 裁剪后的宽高比
    std: float        # 归一化后标准差（判空白页）
    ink: float        # 墨迹像素占比
    px: int = 0       # 原图分辨率（像素数），用于「建议保留」判断画质
    blank: bool = False

    def tile_arr(self) -> np.ndarray:
        return np.frombuffer(self.tile, dtype=np.uint8).reshape(TILE, TILE)

    def map16_arr(self) -> np.ndarray:
        return np.frombuffer(self.map16, dtype=np.uint8).reshape(TILE_SMALL, TILE_SMALL)


def page_tile(norm: np.ndarray) -> np.ndarray:
    """把归一化后的整页压成 TILE×TILE 灰度页图块。"""
    t = cv2.resize(norm, (TILE, TILE), interpolation=cv2.INTER_AREA)
    return np.clip(t, 0, 255).astype(np.uint8)


def page_map16(norm: np.ndarray) -> np.ndarray:
    """16×16 粗筛图（带模糊，提高对残余错位的容忍度）。"""
    m = cv2.resize(norm, (TILE_SMALL, TILE_SMALL), interpolation=cv2.INTER_AREA)
    m = cv2.GaussianBlur(m, (0, 0), sigmaX=1.0)
    return np.clip(m, 0, 255).astype(np.uint8)


def phash_bits(norm: np.ndarray) -> int:
    """64 位感知哈希：32x32 → DCT → 左上 8x8（去掉 DC）→ 中位数二值化。

    用 cv2.dct 自己实现，不依赖 imagehash（少一个可选依赖）。
    """
    p = cv2.resize(norm, (32, 32), interpolation=cv2.INTER_AREA).astype(np.float32)
    d = cv2.dct(p)
    low = d[:PHASH_LOW, :PHASH_LOW].ravel()
    vals = low[1:]                       # 丢掉 DC，只用 63 个交流低频系数
    med = float(np.median(vals))         # 中位数二值化
    bits = vals > med
    out = 0
    for i, b in enumerate(bits):
        if b:
            out |= (1 << i)
    return int(out)


def bhash32_bits(map16: np.ndarray) -> int:
    """32 位版式剖面哈希：16 位来自「逐行墨量」，16 位来自「逐列墨量」。

    对 16×16 粗筛图取行均值 / 列均值，各自与自己的中位数比较，
    得到「上半边墨多还是下半边墨多 / 左半边还是右半边」这类版式剖面。

    这正是扫描漫画指纹论文里最有效的思路 —— **用分镜的位置分布做指纹**，
    因为它完全是结构信息，天然不受纸张底色、光照渐变、噪点影响。
    """
    m = map16.astype(np.float32)
    rows = m.mean(axis=1)
    cols = m.mean(axis=0)
    out = 0
    rmed = float(np.median(rows))
    cmed = float(np.median(cols))
    for i, v in enumerate(rows):
        if v > rmed:
            out |= (1 << i)
    for i, v in enumerate(cols):
        if v > cmed:
            out |= (1 << (16 + i))
    return int(out)


def page_feature(data: bytes, idx: int, do_deskew: bool = True,
                 crop_mode: str = DEFAULT_CROP, work_max: int = WORK_MAX) -> Optional[PageFeat]:
    """从图片字节算出单页特征。任何一步失败返回 None（调用方跳过该页，程序不崩）。"""
    gray = decode_gray(data, work_max=work_max)
    if gray is None:
        return None
    ow, oh = image_size(data)
    try:
        g = auto_crop(gray, mode=crop_mode)
        if do_deskew:
            ang = estimate_skew(g)
            if ang != 0.0:
                g = rotate(g, ang)
        h, w = g.shape[:2]
        if h < 16 or w < 16:
            return None
        g = canon_size(g)
        n = normalize(g)
        ink = float((n < 128).mean())
        std = float(n.std())
        blank = bool(std < BLANK_STD or ink < BLANK_INK)
        tile = page_tile(n)
        map16 = page_map16(n)
        return PageFeat(
            idx=int(idx),
            phash=phash_bits(n),
            bhash32=bhash32_bits(map16),
            tile=tile.tobytes(),
            map16=map16.tobytes(),
            aspect=float(w) / float(max(h, 1)),
            std=std,
            ink=ink,
            px=int(ow) * int(oh),
            blank=blank,
        )
    except Exception:
        return None


# ------------------------------------------------------------------ 相似度


def _popcount(x: np.ndarray) -> np.ndarray:
    """按位计数（numpy 2.0+ 有 bitwise_count，老版本走查表）。"""
    fn = getattr(np, "bitwise_count", None)
    if fn is not None:
        return fn(x).astype(np.int32)
    x = np.asarray(x, dtype=np.uint64)
    table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    out = np.zeros(x.shape, dtype=np.int32)
    for shift in range(0, 64, 8):
        out += table[((x >> np.uint64(shift)) & np.uint64(0xFF)).astype(np.uint8)]
    return out


popcount64 = _popcount
"""64 位数组的按位计数（公开别名）。"""


def popcount32(x: np.ndarray) -> np.ndarray:
    fn = getattr(np, "bitwise_count", None)
    if fn is not None:
        return fn(np.asarray(x, dtype=np.uint32)).astype(np.int32)
    x = np.asarray(x, dtype=np.uint32)
    table = np.array([bin(i).count("1") for i in range(256)], dtype=np.uint8)
    out = np.zeros(x.shape, dtype=np.int32)
    for shift in range(0, 32, 8):
        out += table[((x >> np.uint32(shift)) & np.uint32(0xFF)).astype(np.uint8)]
    return out


def phash_hamming(a: int, b: int) -> int:
    return int(bin((int(a) ^ int(b)) & 0xFFFFFFFFFFFFFFFF).count("1"))


def sim_from_hamming(dist: int, bits: int = 32) -> float:
    """把汉明距离映射到 0~1：0 位差异 = 1.0，bits 位（随机水平）≈ 0.0。"""
    return max(0.0, 1.0 - dist / float(bits))


def aligned_similarity(ta: np.ndarray, tb: np.ndarray, tpl: int = TPL) -> float:
    """**平移不变**的页级相似度 —— 本项目的核心度量。

    做法：把 B 的中心 tpl×tpl 当作模板，在 A 上用归一化相关（TM_CCOEFF_NORMED）
    滑窗搜索最佳位置，峰值就是「允许平移后两张图有多像」。

    ⚠ 为什么非要这样：任何自动裁剪都不可能让两个版本裁得完全一致
    （扫描版有纸边/盖板/歪斜，DL 版干净且可能多留一圈白边），
    残余的错位会把逐格比对的分辨力彻底毁掉 —— 实测裁剪「越准」反而越差，
    因为「扫描版噪声大、DL 版干净」会让两版的裁剪量不一致，引入更大的相对尺度差。
    与其追求完美裁剪，不如让**度量自己把错位搜出来**：同页最低分从 0.24 提到 0.65，
    异页 p99 只有 0.43，从「完全不可分」变成「可以判」。

    ⚠ 输入必须是已归一化的 uint8 图块。函数内部不再做 ``astype(float32)`` 拷贝 ——
    那个拷贝在精比热路径上被重复执行了百万次（实测占总耗时的一半），
    改由调用方一次性转好，这里直接吃 float32。
    """
    a = ta if ta.dtype == np.float32 else ta.astype(np.float32)
    b = tb if tb.dtype == np.float32 else tb.astype(np.float32)
    n = a.shape[0]
    if tpl >= n:
        tpl = n
    off = (n - tpl) // 2
    t = b[off:off + tpl, off:off + tpl]
    if float(t.std()) < 1e-3 or float(a.std()) < 1e-3:
        return 0.0
    r = cv2.matchTemplate(a, t, cv2.TM_CCOEFF_NORMED)
    return float(max(0.0, float(r.max())))


def aligned_similarity_tiles(A: np.ndarray, B: np.ndarray, tpl: int = TPL) -> np.ndarray:
    """批量精比：A (m,TILE,TILE) / B (n,TILE,TILE) uint8 → (m,n) 分数矩阵。"""
    if A.size == 0 or B.size == 0:
        return np.zeros((A.shape[0], B.shape[0]), dtype=np.float32)
    out = np.zeros((A.shape[0], B.shape[0]), dtype=np.float32)
    for i in range(A.shape[0]):
        for j in range(B.shape[0]):
            out[i, j] = aligned_similarity(A[i], B[j], tpl)
    return out


def coarse_similarity_maps(A: np.ndarray, B: np.ndarray) -> np.ndarray:
    """粗筛：16×16 粗筛图上的去均值余弦（矩阵乘法，极快）。

    A (m, N*N) / B (n, N*N) uint8 → (m,n) float32。
    模糊过的 16×16 图对 ±1 格（±6% 幅面）错位不敏感，适合当精比前的短名单筛子。
    """
    if A.size == 0 or B.size == 0:
        return np.zeros((A.shape[0], B.shape[0]), dtype=np.float32)
    a = A.astype(np.float32)
    b = B.astype(np.float32)
    a -= a.mean(axis=1, keepdims=True)
    b -= b.mean(axis=1, keepdims=True)
    na = np.sqrt((a * a).sum(axis=1, keepdims=True))
    nb = np.sqrt((b * b).sum(axis=1, keepdims=True))
    na[na < 1e-3] = np.inf
    nb[nb < 1e-3] = np.inf
    a /= na
    b /= nb
    return np.clip(a @ b.T, 0.0, 1.0).astype(np.float32)


def page_similarity(fa: PageFeat, fb: PageFeat) -> float:
    """单页相似度（平移不变）。"""
    return aligned_similarity(fa.tile_arr(), fb.tile_arr())


# 精比分数与 pHash 通道的融合权重（pHash 只在精比分数偏低时提供补充证据）
W_ALIGN = 0.75
W_PHASH = 0.25


def fused_page_score(align: float, phash_sim: float) -> float:
    """把平移精比分数与 pHash 通道融合成一个 0~1 分数。"""
    return W_ALIGN * float(align) + W_PHASH * float(phash_sim)
