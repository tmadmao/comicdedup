"""诊断：打印同一页两个版本（干净 DL 版 vs 扫描版）的裁剪框、宽高比与倾斜角，
并把「归一化图 + 比对图块」并排存成一张图，肉眼核对流程每一步。

产出 ``tools/_debug_pair.png``（前 4 页 + 所有相似度 < 0.5 的样本行）。

用法：
    python tools/probe_pair.py            # 12 页
    python tools/probe_pair.py 24

⚠ 与 ``probe_align.py`` 的分工：align 管「判据够不够用」（分数分布），
这个管「单页每一步长什么样」（裁框、宽高比、倾斜角、图块）。
两者都收口到出厂默认的 ``core.DEFAULT_CROP``。
"""
from __future__ import annotations

import sys
from pathlib import Path

import cv2
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comicdedup import core  # noqa: E402
from make_testdata import make_manga_page, to_dl, to_jpeg, to_scan  # noqa: E402

OUT = Path(__file__).resolve().parent / "_debug_pair.png"


def chain(im, quality: int):
    """走一遍流水线，把每一步的中间结果都带出来，便于逐段核对。"""
    g0 = core.decode_gray(to_jpeg(im, quality))
    box = core.crop_box(g0, core.DEFAULT_CROP)
    g1 = core.auto_crop(g0, core.DEFAULT_CROP)
    ang = core.estimate_skew(g1)
    g2 = core.rotate(g1, ang) if ang else g1
    g3 = core.canon_size(g2)
    n = core.normalize(g3)
    return dict(raw=g0, box=box, crop=g1, ang=ang, canon=g3, norm=n,
                tile=core.page_tile(n), map16=core.page_map16(n),
                aspect=g3.shape[1] / float(g3.shape[0]))


def hstack_norm(items, size=300):
    out = []
    for lb, im in items:
        a = np.asarray(im)
        if a.dtype != np.uint8:
            a = np.clip(a, 0, 255).astype(np.uint8)
        a = cv2.resize(a, (size, int(size * 1.43)), interpolation=cv2.INTER_AREA)
        a = cv2.copyMakeBorder(a, 20, 4, 4, 4, cv2.BORDER_CONSTANT, value=170)
        cv2.putText(a, lb, (6, 15), cv2.FONT_HERSHEY_SIMPLEX, 0.45, 0, 1, cv2.LINE_AA)
        out.append(a)
    return np.hstack(out)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 12
    rows, stats = [], []
    for i in range(n):
        p = make_manga_page(1000 + i)
        dl = to_dl(p, 400 + i, extra_margin=[0, 24, 60, 0, 18, 40][i % 6])
        sc = to_scan(p, 500 + i, skew=1.4 if i % 2 == 0 else -1.5)
        A, B = chain(dl, 88), chain(sc, 86)
        # 同页两版相似度：走完整流水线 vs 完全不裁剪（只缩放归一）
        f_full = core.aligned_similarity(A["tile"], B["tile"])
        nA = core.normalize(core.canon_size(A["raw"]))
        nB = core.normalize(core.canon_size(B["raw"]))
        f_nocrop = core.aligned_similarity(core.page_tile(nA), core.page_tile(nB))
        stats.append((i, f_full, f_nocrop, A["aspect"], B["aspect"],
                      A["box"], B["box"], A["ang"], B["ang"]))
        if i < 4 or f_full < 0.5:
            rows.append(hstack_norm([
                ("DL-raw", A["raw"]), ("DL-canon", A["canon"]), ("DL-tile", A["tile"]),
                ("DL-map16", A["map16"]),
                ("SC-raw", B["raw"]), ("SC-canon", B["canon"]), ("SC-tile", B["tile"]),
                ("SC-map16", B["map16"]),
            ]))

    print(f"{'i':>2} {'流水线':>8} {'不裁剪':>8} {'宽高比DL':>9} {'宽高比SC':>9} "
          f"{'DL裁框':>22} {'SC裁框':>22} {'角度':>10}")
    for s in stats:
        i, f, fc, aa, ba, bx1, bx2, anga, angb = s
        print(f"{i:>2} {f:>8.3f} {fc:>8.3f} {aa:>9.4f} {ba:>9.4f} "
              f"{str(bx1):>22} {str(bx2):>22} {anga:>+5.1f}/{angb:>+5.1f}")

    full = np.array([s[1] for s in stats])
    nocrop = np.array([s[2] for s in stats])
    print(f"\n同页相似度：流水线 中位 {np.median(full):.3f} 最低 {full.min():.3f} | "
          f"不裁剪 中位 {np.median(nocrop):.3f} 最低 {nocrop.min():.3f}")
    print("（最低分明显低于中位＝有离群页，下面输出的图里最后几行就是它们）")

    if rows:
        sep = np.full((6, rows[0].shape[1]), 40, np.uint8)
        big = rows[0]
        for r in rows[1:]:
            big = np.vstack([big, sep, r])
        cv2.imwrite(str(OUT), big)
        print(f"\n已输出 {OUT}（{len(rows)} 行，含全部低分样本）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
