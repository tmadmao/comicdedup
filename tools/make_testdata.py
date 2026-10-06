"""生成漫画查重测试语料（合成，不涉及任何真实版权内容）。

产出三层验证用的数据：

1. **同书两版本**：同一页的「合成扫描版」与「官方 DL 版」—— 扫描版有黄纸底、
   光照渐变、扫描仪黑边、歪斜、噪点、散焦与网纹；DL 版干净、裁切位置还略有偏移。
   这是本工具最难的一类重复，专门用来标定阈值。
2. **同源变体**：改名 / 换压缩格式（zip→7z→rar）/ 重新压缩 JPEG / 换图片格式。
3. **无关样本**：版式完全不同的其它页，用来确认不会误报。

用法：
    python tools/make_testdata.py --out testdata
"""

from __future__ import annotations

import argparse
import io
import os
import random
import shutil
import subprocess
import sys
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageFilter

PAGE_W, PAGE_H = 1500, 2150


class Rng(random.Random):
    """random.Random 加上 numpy 风格的 integers(a, b)（左闭右开）。"""

    def integers(self, a, b):
        return self.randint(int(a), int(b) - 1)


# ------------------------------------------------------------------ 页面生成


def _draw_content(img, x0, y0, x1, y1, rng, dense=False):
    """在一个分镜内画内容：线条、网点、涂黑、速度线。"""
    w, h = x1 - x0, y1 - y0
    if w < 24 or h < 24:
        return
    kind = rng.random()
    if kind < 0.30:  # 网点渐变区（真实漫画最常见的灰面）
        gx = int(x0 + rng.integers(4, max(5, w // 3)))
        gy = int(y0 + rng.integers(4, max(5, h // 3)))
        gw = int(min(w - 6, rng.integers(w // 4 + 8, max(w // 3, w // 2))))
        gh = int(min(h - 6, rng.integers(h // 4 + 8, max(h // 3, h // 2))))
        seg = img[gy:gy + gh, gx:gx + gw]
        if seg.size > 64:
            step = int(rng.integers(4, 9))
            dots = np.zeros(seg.shape, np.uint8)
            dots[::step, ::step] = 255
            k = int(rng.integers(2, 4))
            dots = cv2.dilate(dots, np.ones((k, k), np.uint8))
            seg[dots > 0] = int(rng.integers(0, 60))
    elif kind < 0.45:  # 大面积涂黑（冲击画面）
        bx = int(x0 + rng.integers(0, max(1, w // 2)))
        by = int(y0 + rng.integers(0, max(1, h // 2)))
        bw = int(rng.integers(max(8, w // 5), max(9, w - (bx - x0))))
        bh = int(rng.integers(max(8, h // 5), max(9, h - (by - y0))))
        cv2.rectangle(img, (bx, by), (bx + bw, by + bh), 0, -1)
    elif kind < 0.60:  # 速度线：中心放射
        cx, cy = int(x0 + w // 2), int(y0 + h // 2)
        for _ in range(int(rng.integers(10, 26))):
            a = rng.uniform(0, 6.283)
            r0 = rng.uniform(0.1, 0.35) * min(w, h)
            r1 = rng.uniform(0.5, 0.72) * min(w, h)
            cv2.line(img, (int(cx + r0 * np.cos(a)), int(cy + r0 * np.sin(a))),
                     (int(cx + r1 * np.cos(a)), int(cy + r1 * np.sin(a))), 0,
                     int(rng.integers(1, 3)))
    # 通用：轮廓线 / 人物简笔
    for _ in range(int(rng.integers(4, 12) if not dense else rng.integers(10, 22))):
        n = int(rng.integers(3, 8))
        pts = np.array([[int(x0 + rng.integers(2, max(3, w - 2))),
                         int(y0 + rng.integers(2, max(3, h - 2)))] for _ in range(n)], np.int32)
        cv2.polylines(img, [pts], bool(rng.random() < 0.35), 0, int(rng.integers(2, 5)))
    # 台词框
    if rng.random() < 0.55 and w > 90 and h > 70:
        bw = int(w * rng.uniform(0.30, 0.52))
        bh = int(h * rng.uniform(0.12, 0.22))
        bx = int(x0 + rng.integers(6, max(7, w - bw - 6)))
        by = int(y0 + rng.integers(6, max(7, h - bh - 6)))
        cv2.ellipse(img, (bx + bw // 2, by + bh // 2), (bw // 2, max(6, bh // 2)),
                    0, 0, 360, 255, -1)
        cv2.ellipse(img, (bx + bw // 2, by + bh // 2), (bw // 2, max(6, bh // 2)),
                    0, 0, 360, 0, 3)
        for i in range(int(max(2, bh // 22))):
            yy = by + bh // 2 - bh // 3 + i * 20
            cv2.line(img, (bx + 16, yy), (bx + bw - 16, yy), 0, 2)


def _split(rect, rng, depth):
    """递归把矩形切成若干分镜（真实漫画的版式就是这么来的）。"""
    x0, y0, x1, y1 = rect
    w, h = x1 - x0, y1 - y0
    if depth <= 0 or w < 190 or h < 190 or rng.random() < 0.18:
        return [rect]
    gutter = int(rng.integers(14, 46))
    if (w > h and rng.random() < 0.72) or (h > w and rng.random() < 0.28):
        n = int(rng.integers(2, 4)) if w > 520 else 2
        cuts = sorted(rng.uniform(0.28, 0.72) for _ in range(n - 1))
        out, prev = [], 0.0
        for i, c in enumerate(list(cuts) + [1.0]):
            cx0 = int(x0 + prev * w)
            cx1 = int(x0 + c * w) - (gutter // 2 if i < len(cuts) else 0)
            if cx1 - cx0 > 60:
                out += _split((cx0, y0, cx1, y1), rng, depth - 1)
            prev = c
        return out or [rect]
    n = int(rng.integers(2, 4)) if h > 700 else 2
    cuts = sorted(rng.uniform(0.28, 0.72) for _ in range(n - 1))
    out, prev = [], 0.0
    for i, c in enumerate(list(cuts) + [1.0]):
        cy0 = int(y0 + prev * h)
        cy1 = int(y0 + c * h) - (gutter // 2 if i < len(cuts) else 0)
        if cy1 - cy0 > 60:
            out += _split((x0, cy0, x1, cy1), rng, depth - 1)
        prev = c
    return out or [rect]


def make_manga_page(seed: int) -> Image.Image:
    """造一页干净漫画（相当于官方 DL 版的原图）。"""
    rng = Rng(seed)
    img = np.full((PAGE_H, PAGE_W), 255, np.uint8)
    margin = rng.choice([26, 40, 56, 70, 92, 120])
    rects = _split((margin, margin, PAGE_W - margin, PAGE_H - margin), rng,
                   rng.choice([1, 2, 2, 3, 3, 4]))
    for r in rects:
        x0, y0, x1, y1 = r
        if rng.random() < 0.12:  # 出血分镜：直接画内容不画框
            _draw_content(img, x0, y0, x1, y1, rng, dense=True)
            continue
        cv2.rectangle(img, (x0, y0), (x1, y1), 0, int(rng.integers(3, 7)))
        _draw_content(img, x0 + 5, y0 + 5, x1 - 5, y1 - 5, rng)
    if rng.random() < 0.25:  # 整页边框
        cv2.rectangle(img, (margin - 8, margin - 8), (PAGE_W - margin + 8, PAGE_H - margin + 8), 0, 4)
    return Image.fromarray(img, "L")


# ------------------------------------------------------------------ 两个版本


def to_scan(pil: Image.Image, seed: int, skew: float = 1.4, pad: int = 44,
            paper: int = 198, shadow: float = 0.30) -> Image.Image:
    """把干净页变成「自制书本扫描版」。

    模拟：黄纸底色、光照渐变阴影、扫描仪黑边、歪斜、噪点、散焦、JPEG 风格劣化。
    """
    rng = np.random.default_rng(seed)
    a = np.asarray(pil).astype(np.float32)
    h, w = a.shape
    # 纸面不再纯白，墨迹也不再纯黑（扫描的动态范围被压缩）
    a = np.where(a > 127, float(paper), a * (float(paper) / 255.0) + 9.0)
    # 纸张偏黄：用通道化模拟后再转灰，最终灰度上表现为轻微色偏 + 底色不均
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    grad = 1.0 - shadow * (0.55 * (xx / w) + 0.55 * (yy / h))          # 对角渐变
    grad *= 1.0 - 0.06 * np.sin(xx / w * 3.1)                          # 装订方向的周期性阴影
    a *= grad
    canvas = np.full((h + 2 * pad, w + 2 * pad), 21.0, np.float32)     # 扫描仪盖板黑边
    canvas[pad:pad + h, pad:pad + w] = a
    canvas += rng.normal(0, 6.5, canvas.shape).astype(np.float32)      # 传感器噪点
    canvas = np.clip(canvas, 0, 255).astype(np.uint8)
    out = Image.fromarray(canvas, "L")
    nw, nh = out.size
    ang = float(skew + rng.normal(0, 0.25))
    m = cv2.getRotationMatrix2D((nw / 2.0, nh / 2.0), ang, 1.0)
    out = Image.fromarray(cv2.warpAffine(np.asarray(out), m, (nw, nh), flags=cv2.INTER_LINEAR,
                                         borderMode=cv2.BORDER_CONSTANT, borderValue=23), "L")
    out = out.filter(ImageFilter.GaussianBlur(round(rng.uniform(0.5, 1.1), 2)))
    return out


def to_dl(pil: Image.Image, seed: int, extra_margin: int = 0) -> Image.Image:
    """把干净页变成「官方 DL 版」。

    特点：干净、纯白、无黑边，但**裁切位置与扫描版不同**——可能多留一圈白边，
    也可能整体平移几个像素。这正是跨版本比对最容易踩的坑。
    """
    rng = Rng(seed)
    a = np.asarray(pil)
    h, w = a.shape
    if extra_margin:
        a = cv2.copyMakeBorder(a, extra_margin, extra_margin, extra_margin, extra_margin,
                               cv2.BORDER_CONSTANT, value=255)
        h, w = a.shape
    dx, dy = rng.randint(-9, 9), rng.randint(-9, 9)
    m = np.float32([[1, 0, dx], [0, 1, dy]])
    a = cv2.warpAffine(a, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_CONSTANT,
                       borderValue=255)
    out = Image.fromarray(a, "L")
    if rng.random() < 0.5:
        out = out.resize((int(w * rng.uniform(0.85, 1.05)), int(h * rng.uniform(0.85, 1.05))),
                         Image.LANCZOS)
    return out


def to_jpeg(pil: Image.Image, quality: int = 92) -> bytes:
    b = io.BytesIO()
    pil.convert("L").save(b, format="JPEG", quality=quality, optimize=False)
    return b.getvalue()


def to_png(pil: Image.Image) -> bytes:
    b = io.BytesIO()
    pil.convert("L").save(b, format="PNG", compress_level=6)
    return b.getvalue()


def to_webp(pil: Image.Image, quality: int = 85) -> bytes:
    b = io.BytesIO()
    pil.convert("L").save(b, format="WEBP", quality=quality, method=4)
    return b.getvalue()


# ------------------------------------------------------------------ 写语料

SEVENZIP_CANDIDATES = [
    r"C:\Program Files\7-Zip\7z.exe",
    r"C:\Program Files\7-Zip-Zstandard\7z.exe",
    r"C:\Program Files (x86)\7-Zip\7z.exe",
    "7z", "7za",
]
RAR_CANDIDATES = [
    r"C:\Program Files\WinRAR\Rar.exe",
    r"C:\Program Files (x86)\WinRAR\Rar.exe",
    "rar",
]


def _find(cands):
    for c in cands:
        if os.path.sep in c:
            if Path(c).exists():
                return c
        else:
            p = shutil.which(c)
            if p:
                return p
    return None


def write_zip(path: Path, files):
    import zipfile
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files:
            z.writestr(name, data)


def write_7z(path: Path, files, sevenz: str):
    tmp = path.with_suffix(".tmpdir")
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for name, data in files:
            (tmp / name).write_bytes(data)
        subprocess.run([sevenz, "a", "-t7z", "-mx=3", "-bso0", "-bsp0", str(path), "."],
                       cwd=str(tmp), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def write_rar(path: Path, files, rar: str):
    tmp = path.with_suffix(".tmpdir")
    tmp.mkdir(parents=True, exist_ok=True)
    try:
        for name, data in files:
            (tmp / name).write_bytes(data)
        subprocess.run([rar, "a", "-ep1", "-m3", "-idq", str(path), "."],
                       cwd=str(tmp), check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def build(out: Path):
    sevenz = _find(SEVENZIP_CANDIDATES)
    rar = _find(RAR_CANDIDATES)
    print(f"7z  = {sevenz}")
    print(f"rar = {rar}")
    if out.exists():
        shutil.rmtree(out, ignore_errors=True)
    out.mkdir(parents=True)

    # ---- A 组：同一本书的「扫描版」与「官方 DL 版」（8 页/本）
    book_pages = [make_manga_page(1000 + i) for i in range(8)]
    dl_jpg = [to_jpeg(to_dl(p, 400 + i, extra_margin=[0, 0, 24, 0, 60, 0, 0, 18][i]), 90)
              for i, p in enumerate(book_pages)]
    scan_jpg = [to_jpeg(to_scan(p, 500 + i, skew=[1.4, -1.8, 0.9, 2.3, -0.7, 1.1, -2.1, 1.7][i]), 66)
                for i, p in enumerate(book_pages)]

    # 扫描版：zip；DL 版：7z —— 跨格式 + 跨版本，最难的组合
    write_zip(out / "火影忍者 第01卷 [自制扫描].zip",
              [(f"{i+1:03d}.jpg", d) for i, d in enumerate(scan_jpg)])
    write_7z(out / "火影忍者 第01卷 [官方DL].7z",
             [(f"{i+1:03d}.jpg", d) for i, d in enumerate(dl_jpg)], sevenz)

    # ---- B 组：同源三变体
    src = [to_jpeg(make_manga_page(2000 + i), 88) for i in range(8)]
    write_zip(out / "海贼王 第10卷.zip", [(f"p{i+1:02d}.jpg", d) for i, d in enumerate(src)])
    write_zip(out / "海贼王 第10卷 (改名副本).zip", [(f"page_{i+1}.jpg", d) for i, d in enumerate(src)])
    write_7z(out / "海贼王 第10卷 (重打包).7z", [(f"{i+1:03d}.jpg", d) for i, d in enumerate(src)], sevenz)

    # 换成 webp 图片、重新压缩 —— 模拟「图片压缩不同」
    if True:
        pages = [make_manga_page(2100 + i) for i in range(8)]
        write_zip(out / "进击的巨人 第05卷 (原始).zip",
                  [(f"{i+1:03d}.jpg", to_jpeg(p, 95)) for i, p in enumerate(pages)])
        write_zip(out / "进击的巨人 第05卷 (webp低清).zip",
                  [(f"{i+1:03d}.webp", to_webp(p, 62)) for i, p in enumerate(pages)])

    # ---- C 组：图片文件夹形态的两本
    pages = [make_manga_page(2200 + i) for i in range(8)]
    d1 = out / "死神 第03卷" / "死神 第03卷"
    d1.mkdir(parents=True)
    for i, p in enumerate(pages):
        (d1 / f"{i+1:03d}.jpg").write_bytes(to_jpeg(p, 88))
    d2 = out / "死神 第03卷 (DL文件夹)"
    d2.mkdir(parents=True)
    for i, p in enumerate(pages):
        (d2 / f"{i+1:03d}.png").write_bytes(to_png(to_dl(p, 700 + i, 30)))
    # 文件夹里混入损坏图片与垃圾文件
    (d2 / "坏图.jpg").write_bytes(b"\xff\xd8\xff\xe0 this is not a real jpeg")
    (d2 / "readme.txt").write_text("not an image", encoding="utf-8")

    # ---- D 组：无关样本（版式各不同，不能和上面任何一本成组）
    for k in range(6):
        pages = [make_manga_page(3000 + k * 20 + i) for i in range(8)]
        write_zip(out / f"无关漫画 {k+1:02d}.zip",
                  [(f"{i+1:03d}.jpg", to_jpeg(p, 88)) for i, p in enumerate(pages)])

    # ---- E 组：同一系列不同卷（共享封面设计，专门验证不会连环并组）
    #      三本各 8 页，其中前 2 页用同一套「系列页」，其余页完全不同。
    series_shared = [make_manga_page(4000), make_manga_page(4001)]
    for vol in range(3):
        pages = list(series_shared) + [make_manga_page(4100 + vol * 10 + i) for i in range(6)]
        write_zip(out / f"某系列 第{vol+1:02d}卷.zip",
                  [(f"{i+1:03d}.jpg", to_jpeg(p, 88)) for i, p in enumerate(pages)])

    # ---- F 组：单本（无重复）
    pages = [make_manga_page(5000 + i) for i in range(8)]
    write_zip(out / "孤独的一本.zip", [(f"{i+1:03d}.jpg", to_jpeg(p, 88)) for i, p in enumerate(pages)])

    # ---- G 组：损坏压缩包（验证跳过 + 记日志，不崩）
    (out / "彻底损坏.zip").write_bytes(b"PK\x03\x04" + os.urandom(5000))
    if rar:
        write_rar(out / "妖精的尾巴 第07卷 (rar).rar",
                  [(f"{i+1:03d}.jpg", to_jpeg(p, 85))
                   for i, p in enumerate([make_manga_page(6000 + i) for i in range(8)])], rar)

    n = sum(1 for _ in out.rglob("*") if _.is_file())
    print(f"语料已生成：{out}  文件数 {n}")
    for p in sorted(out.iterdir()):
        print("   ", p.name)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="testdata")
    a = ap.parse_args()
    root = Path(a.out)
    if not root.is_absolute():
        root = Path(__file__).resolve().parent.parent / root
    build(root)


if __name__ == "__main__":
    main()
