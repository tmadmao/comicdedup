"""诊断：打印同页两个版本的裁剪框/宽高比，并把归一化图与密度图并排存图。"""
import sys

import cv2
import numpy as np

sys.path.insert(0, r"E:\pj\manhuachachong")
sys.path.insert(0, r"E:\pj\manhuachachong\tools")
from comicdedup import core  # noqa: E402
from make_testdata import make_manga_page, to_dl, to_jpeg, to_scan  # noqa: E402


def chain(im):
    """返回每一步的中间结果，便于逐段核对。"""
    g0 = core.decode_gray(to_jpeg(im, 80 if im else 90))
    box = core.crop_box(g0)
    g1 = core.auto_crop(g0)
    ang = core.estimate_skew(g1)
    g2 = core.rotate(g1, ang) if ang else g1
    box2 = core.crop_box(g2)
    g3 = core.auto_crop(g2)
    g4 = core.canon_size(g3)
    n = core.normalize(g4)
    return dict(raw=g0, box=box, crop=g1, ang=ang, box2=box2, crop2=g3, canon=g4, norm=n,
                aspect=g3.shape[1] / g3.shape[0],
                dens=core.density_map(n))


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
    rows = []
    stats = []
    for i in range(n):
        p = make_manga_page(1000 + i)
        dl = to_dl(p, 400 + i, extra_margin=[0, 24, 60, 0, 18, 40][i % 6])
        sc = to_scan(p, 500 + i, skew=1.4 if i % 2 == 0 else -1.5)
        A, B = chain(dl), chain(sc)
        f = core.dens_similarity(A["dens"], B["dens"])
        f_canned = core.dens_similarity(core.density_map(core.normalize(core.canon_size(A["raw"]))),
                                        core.density_map(core.normalize(core.canon_size(B["raw"]))))
        stats.append((i, f, f_canned, A["aspect"], B["aspect"], A["box"], B["box"],
                      A["box2"], B["box2"], A["ang"], B["ang"]))
        if i < 4 or f < 0.5:
            rows.append(hstack_norm([
                ("DL-raw", A["raw"]), ("DL-crop", A["crop2"]), ("DL-dens", A["dens"]),
                ("SC-raw", B["raw"]), ("SC-crop", B["crop2"]), ("SC-dens", B["dens"]),
            ]))

    print(f"{'i':>2} {'裁剪后密度':>8} {'不裁剪密度':>8} {'宽高比DL':>8} {'宽高比SC':>8} "
          f"{'DL box':>22} {'SC box':>22} {'box2DL':>18} {'box2SC':>18} {'角度':>6}")
    for s in stats:
        i, f, fc, aa, ba, bx1, bx2, b2a, b2b, anga, angb = s
        print(f"{i:>2} {f:>8.3f} {fc:>8.3f} {aa:>8.4f} {ba:>8.4f} {str(bx1):>22} {str(bx2):>22} "
              f"{str(b2a):>18} {str(b2b):>18} {anga:>3.1f}/{angb:>3.1f}")

    if rows:
        sep = np.full((6, rows[0].shape[1]), 40, np.uint8)
        big = rows[0]
        for r in rows[1:]:
            big = np.vstack([big, sep, r])
        cv2.imwrite(r"E:\pj\manhuachachong\tools\_debug_pair.png", big)
        print(f"\n已输出 tools/_debug_pair.png（{len(rows)} 行，含全部失败样本）")


if __name__ == "__main__":
    main()
