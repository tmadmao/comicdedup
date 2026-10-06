"""阈值标定探针：统计「同页（扫描版 vs DL版）」与「异页」的相似度分布。

用大量随机版式页面跑，给出分位数，用来定页级阈值与权重。
"""
import sys
import time

import numpy as np

sys.path.insert(0, r"E:\pj\manhuachachong")
sys.path.insert(0, r"E:\pj\manhuachachong\tools")
from comicdedup import core  # noqa: E402
from make_testdata import make_manga_page, to_dl, to_jpeg, to_scan  # noqa: E402


def feats(pairs, do_deskew=True):
    out = []
    for tag, im, q in pairs:
        f = core.page_feature(to_jpeg(im, q), 0, do_deskew=do_deskew)
        out.append((tag, f))
    return out


def show(name, arr):
    a = np.asarray(arr)
    print(f"  {name:26s} n={len(a):4d} min={a.min():.3f} p1={np.percentile(a,1):.3f} "
          f"p5={np.percentile(a,5):.3f} 中位={np.median(a):.3f} p95={np.percentile(a,95):.3f} "
          f"max={a.max():.3f}")


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    t0 = time.time()
    pages = [make_manga_page(1000 + i) for i in range(n)]
    dl = [to_dl(p, 400 + i, extra_margin=[0, 24, 60, 0, 18, 40][i % 6]) for i, p in enumerate(pages)]
    sc = [to_scan(p, 500 + i, skew=1.4 * (1 if i % 2 == 0 else -1) + 0.3 * (i % 3))
          for i, p in enumerate(pages)]

    fdl, fsc = [], []
    for i in range(n):
        fdl.append(core.page_feature(to_jpeg(dl[i], 90), i))
        fsc.append(core.page_feature(to_jpeg(sc[i], 66), i))
    dt = (time.time() - t0) / (2 * n) * 1000
    print(f"每页特征耗时 ≈ {dt:.0f} ms（{n} 对，含 JPEG 编解码）")

    missing = [i for i in range(n) if fdl[i] is None or fsc[i] is None]
    print(f"特征提取失败：{missing}")

    pa = np.array([fdl[i].phash for i in range(n)], np.uint64)
    pb = np.array([fsc[i].phash for i in range(n)], np.uint64)
    da = np.stack([fdl[i].dens_array().ravel() for i in range(n)])
    db = np.stack([fsc[i].dens_array().ravel() for i in range(n)])
    M = core.page_similarity_many(da, db, pa, pb)

    same = np.array([M[i, i] for i in range(n)])
    diff = np.array([M[i, j] for i in range(n) for j in range(n) if i != j])
    sd_same = np.array([core.dens_similarity(fdl[i].dens_array(), fsc[i].dens_array())
                        for i in range(n)])
    sd_diff = np.array([core.dens_similarity(fdl[i].dens_array(), fsc[j].dens_array())
                        for i in range(n) for j in range(n) if i != j])
    sp_same = np.array([core.sim_from_hamming(core.phash_hamming(fdl[i].phash, fsc[i].phash))
                        for i in range(n)])
    sp_diff = np.array([core.sim_from_hamming(core.phash_hamming(fdl[i].phash, fsc[j].phash))
                        for i in range(n) for j in range(n) if i != j])

    print("\n=== 合并分数（扫描版 vs DL版）===")
    show("同页（应判重）", same)
    show("异页（不应判重）", diff)
    print("\n=== 只有密度图通道 ===")
    show("同页", sd_same)
    show("异页", sd_diff)
    print("\n=== 只有 pHash 通道 ===")
    show("同页", sp_same)
    show("异页", sp_diff)

    # 同源副本（同图不同 JPEG 质量 / 不同格式）
    same_src = []
    for i in range(n):
        b1 = to_jpeg(sc[i], 90)
        b2 = to_jpeg(sc[i], 45)
        f1 = core.page_feature(b1, 0)
        f2 = core.page_feature(b2, 0)
        same_src.append(core.page_similarity(f1, f2))
    print("\n=== 同源重压缩（JPEG 66 vs JPEG 45）===")
    show("同页", np.array(same_src))

    # 建议阈值：取「同页最小」与「异页最大」之间的安全位置
    lo, hi = float(np.min(same)), float(np.max(diff))
    print(f"\n同页最低 {lo:.3f} / 异页最高 {hi:.3f} "
          f"→ {'可分' if lo > hi else '★不可分，需要调权重'}  建议阈值 {(lo + hi) / 2:.3f}")
    print(f"总耗时 {time.time()-t0:.1f}s")


if __name__ == "__main__":
    main()
