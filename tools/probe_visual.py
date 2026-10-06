"""临时探针：把预处理各阶段拼成一张对比图，肉眼确认裁剪/归一化是否对齐。"""
import io
import sys

import cv2
import numpy as np
from PIL import Image

sys.path.insert(0, r"E:\pj\manhuachachong")
from comicdedup import core  # noqa: E402
from probe_core import make_page, to_scan, jpg_bytes  # noqa: E402


def stage(gray, tag):
    """返回一行四张：原图 / 裁剪后 / 归一化后 / 密度图放大。"""
    g = gray
    cr = core.auto_crop(g)
    sk = core.deskew(cr)
    gc = core.auto_crop(sk)
    cn = core.canon_size(gc)
    nm = core.normalize(cn)
    d = core.density_map(nm)
    d = cv2.resize(d, (nm.shape[1], nm.shape[0]), interpolation=cv2.INTER_NEAREST)
    out = []
    for im, lb in ((g, "raw"), (gc, "crop"), (nm, "norm"), (d, "dens")):
        a = np.asarray(im)
        if a.dtype != np.uint8:
            a = np.clip(a, 0, 255).astype(np.uint8)
        a = cv2.resize(a, (300, 430), interpolation=cv2.INTER_AREA)
        a = cv2.copyMakeBorder(a, 22, 4, 4, 4, cv2.BORDER_CONSTANT, value=200)
        cv2.putText(a, lb, (8, 16), cv2.FONT_HERSHEY_SIMPLEX, 0.5, 0, 1, cv2.LINE_AA)
        out.append(a)
    return np.hstack(out)


def main():
    idx = int(sys.argv[1]) if len(sys.argv) > 1 else 0
    clean = make_page(100 + idx)
    scan = to_scan(clean, 200 + idx, skew=1.6)
    # 也做一个不歪斜的扫描版
    scan0 = to_scan(clean, 300 + idx, skew=0.0)

    def dec(b):
        return core.decode_gray(b)

    rows = [
        stage(dec(jpg_bytes(clean, 95)), "clean"),
        stage(dec(jpg_bytes(scan0, 68)), "scan-noskew"),
        stage(dec(jpg_bytes(scan, 68)), "scan-skew"),
    ]
    sep = np.full((6, rows[0].shape[1]), 60, np.uint8)
    big = rows[0]
    for r in rows[1:]:
        big = np.vstack([big, sep, r])
    cv2.imwrite(r"E:\pj\manhuachachong\tools\_debug_stages.png", big)

    # 数值：同页 / 异页
    f = {}
    for name, im in (("clean", clean), ("scan0", scan0), ("scan", scan)):
        f[name] = core.page_feature(jpg_bytes(im, 80 if name != "clean" else 95), 0, do_deskew=True)
    f2 = {}
    for name, im in (("clean", make_page(101 + idx)), ("scan", to_scan(make_page(101 + idx), 500, 1.0))):
        f2[name] = core.page_feature(jpg_bytes(im, 80), 0, do_deskew=True)

    print("同页相似度：")
    for a in ("scan0", "scan"):
        print(f"  clean vs {a:6s} = {core.page_similarity(f['clean'], f[a]):.3f}  "
              f"(密度 {core.dens_similarity(f['clean'].dens_array(), f[a].dens_array()):.3f}, "
              f"pHash {core.sim_from_hamming(core.phash_hamming(f['clean'].phash, f[a].phash)):.3f})")
    print("异页相似度：")
    print(f"  clean vs 别的页 = {core.page_similarity(f['clean'], f2['clean']):.3f}  "
          f"(密度 {core.dens_similarity(f['clean'].dens_array(), f2['clean'].dens_array()):.3f}, "
          f"pHash {core.sim_from_hamming(core.phash_hamming(f['clean'].phash, f2['clean'].phash)):.3f})")
    print(f"  scan  vs 别的页 = {core.page_similarity(f['scan'], f2['scan']):.3f}  "
          f"(密度 {core.dens_similarity(f['scan'].dens_array(), f2['scan'].dens_array()):.3f}, "
          f"pHash {core.sim_from_hamming(core.phash_hamming(f['scan'].phash, f2['scan'].phash)):.3f})")
    for name in ("clean", "scan0", "scan"):
        g = core.decode_gray(jpg_bytes(f[name] and (clean if name == "clean" else scan0 if name == "scan0" else scan), 80))
        print(f"{name}: 倾斜估计 = {core.estimate_skew(core.auto_crop(g)):.2f} 度")
    print("输出：tools/_debug_stages.png")


if __name__ == "__main__":
    main()
