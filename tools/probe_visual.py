"""探针：把预处理各阶段拼成一张对比图，肉眼确认裁剪/归一化是否对齐。

产出 ``tools/_debug_stages.png``（三行：干净版 / 不歪斜的扫描版 / 歪斜扫描版），
每行四列：原图 → 裁剪后 → 归一化后 → 比对用的 16×16 粗筛图（放大显示）。

用法：
    python tools/probe_visual.py            # 第 0 页
    python tools/probe_visual.py 3          # 第 3 页

⚠ 这里刻意只用**出厂默认**的流水线（``core.DEFAULT_CROP`` = bed）。
想看其它裁剪方案长什么样，用 ``tools/probe_align.py --mode=...`` 比数值，
那个才是判据层面对比的地方。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comicdedup import core  # noqa: E402
from make_testdata import make_manga_page, to_jpeg, to_scan  # noqa: E402

OUT = Path(__file__).resolve().parent / "_debug_stages.png"


def stage(gray, tag):
    """把一页走完整条预处理流水线，拼出一行四列对比图。"""
    cropped = core.auto_crop(gray, core.DEFAULT_CROP)
    deskewed = core.deskew(cropped)
    canon = core.canon_size(deskewed)
    norm = core.normalize(canon)
    # 比对真正吃的那张小图（16×16 粗筛图），放大回页面尺寸只是为了看得见
    m16 = core.page_map16(norm)
    big16 = cv2.resize(m16, (norm.shape[1], norm.shape[0]), interpolation=cv2.INTER_NEAREST)

    out = []
    for im, lb in ((gray, "raw"), (canon, "crop+canon"), (norm, "norm"), (big16, "map16 x16")):
        a = np.asarray(im)
        if a.dtype != np.uint8:
            a = np.clip(a, 0, 255).astype(np.uint8)
        a = cv2.resize(a, (300, 430), interpolation=cv2.INTER_AREA)
        a = cv2.copyMakeBorder(a, 22, 4, 4, 4, cv2.BORDER_CONSTANT, value=200)
        cv2.putText(a, f"{tag}/{lb}", (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.42, 0, 1, cv2.LINE_AA)
        out.append(a)
    return np.hstack(out)


def main():
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    clean = make_manga_page(100 + idx)
    other = make_manga_page(101 + idx)          # 完全不同的一页，用来对照
    scan0 = to_scan(clean, 300 + idx, skew=0.0)
    scan = to_scan(clean, 200 + idx, skew=1.6)

    def dec(b: bytes):
        return core.decode_gray(b)

    rows = [
        stage(dec(to_jpeg(clean, 95)), "clean"),
        stage(dec(to_jpeg(scan0, 68)), "scan-noskew"),
        stage(dec(to_jpeg(scan, 68)), "scan-skew"),
    ]
    sep = np.full((6, rows[0].shape[1]), 60, np.uint8)
    big = rows[0]
    for r in rows[1:]:
        big = np.vstack([big, sep, r])
    cv2.imwrite(str(OUT), big)

    # ---- 数值核对（与界面/引擎用的是同一套判据）
    t0 = time.time()
    f = {
        "clean": core.page_feature(to_jpeg(clean, 95), 0),
        "scan0": core.page_feature(to_jpeg(scan0, 68), 0),
        "scan": core.page_feature(to_jpeg(scan, 68), 0),
        "other_clean": core.page_feature(to_jpeg(other, 95), 0),
        "other_scan": core.page_feature(to_jpeg(to_scan(other, 400 + idx, skew=1.0), 68), 0),
    }
    if any(v is None for v in f.values()):
        print("[FAIL] 有页面提不出特征，无法继续")
        return 1

    def line(label, ka, kb):
        a, b = f[ka], f[kb]
        al = core.aligned_similarity(a.tile_arr(), b.tile_arr())
        ph = core.sim_from_hamming(core.phash_hamming(a.phash, b.phash), 32)
        print(f"  {label:26s} 平移精比 {al:.3f}   pHash {ph:.3f}   融合 "
              f"{core.fused_page_score(al, ph):.3f}")

    print(f"裁剪模式={core.DEFAULT_CROP}（出厂默认）  特征耗时 {(time.time()-t0)/len(f)*1000:.0f} ms/页")
    print("同页（应高分）：")
    line("clean vs scan-noskew", "clean", "scan0")
    line("clean vs scan-skew", "clean", "scan")
    print("异页（应低分）：")
    line("clean vs 另一页(干净)", "clean", "other_clean")
    line("scan  vs 另一页(扫描)", "scan", "other_scan")
    print("倾斜估计：")
    for name in ("clean", "scan0", "scan"):
        g = {"clean": clean, "scan0": scan0, "scan": scan}[name]
        q = 95 if name == "clean" else 68
        gray = core.decode_gray(to_jpeg(g, q))
        print(f"  {name:14s} 裁后估计 = "
              f"{core.estimate_skew(core.auto_crop(gray, core.DEFAULT_CROP)):+.2f} 度")
    print(f"输出：{OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
