"""标定探针：用 core 的最终流水线测「同页(扫描版vs官方DL版)」与「异页」的可分性。

用法：
    python tools/probe_align.py 24                    # 出厂默认裁剪模式
    python tools/probe_align.py 24 --mode=edge        # 换一种裁剪方案做对照

⚠ **默认值必须是 ``core.DEFAULT_CROP``**。旧版本把这个变量硬编码成 ``mass``，
于是探针跑出来的是「间距 -0.56、多页投票 0/3 全部失效」——那是一条**已经被否决的
实验路线**的结果，与出厂配置毫无关系。README「4.3 裁剪越准反而越差」那张对照表
里的失败行，只有在显式 ``--mode=mass`` 时才应该被复现出来。
"""
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from comicdedup import core  # noqa: E402
from make_testdata import make_manga_page, to_dl, to_jpeg, to_scan  # noqa: E402


def feat(im, mode, do_deskew=True):
    return core.page_feature(to_jpeg(im, 88), 0, do_deskew=do_deskew, crop_mode=mode)


def main():
    n = int(sys.argv[1]) if len(sys.argv) > 1 else 24
    mode = core.DEFAULT_CROP           # 出厂默认（bed）
    for a in sys.argv:
        if a.startswith("--mode="):
            mode = a.split("=")[1]
            if mode not in ("bed", "mass", "edge", "paper"):
                print(f"[FAIL] 未知裁剪模式 {mode!r}，可选 bed / mass / edge / paper")
                return 2

    pages = [make_manga_page(1000 + i) for i in range(n)]
    dls = [to_dl(p, 400 + i, [0, 24, 60, 0, 18, 40][i % 6]) for i, p in enumerate(pages)]
    scs = [to_scan(p, 500 + i, 1.4 if i % 2 == 0 else -1.5) for i, p in enumerate(pages)]

    t0 = time.time()
    FA = [feat(x, mode) for x in dls]
    FB = [feat(x, mode) for x in scs]
    print(f"裁剪模式={mode}  特征耗时 {(time.time()-t0)/(2*n)*1000:.0f} ms/页  "
          f"空白页 DL={sum(f.blank for f in FA)} SC={sum(f.blank for f in FB)}")

    TA = np.stack([f.tile_arr() for f in FA])
    TB = np.stack([f.tile_arr() for f in FB])
    MA = np.stack([f.map16_arr().ravel() for f in FA])
    MB = np.stack([f.map16_arr().ravel() for f in FB])
    pa = np.array([f.phash for f in FA], np.uint64)
    pb = np.array([f.phash for f in FB], np.uint64)
    ba = np.array([f.bhash32 for f in FA], np.uint32)
    bb = np.array([f.bhash32 for f in FB], np.uint32)

    # --- 平移不变精比
    t1 = time.time()
    AL = core.aligned_similarity_tiles(TA, TB)
    ms = (time.time() - t1) / (n * n) * 1000
    PH = 1.0 - core.popcount64(np.bitwise_xor(pa[:, None], pb[None, :])).astype(np.float32) / 32.0
    FU = core.W_ALIGN * AL + core.W_PHASH * np.clip(PH, 0, 1)
    CO = core.coarse_similarity_maps(MA, MB)
    print(f"精比 {ms:.2f} ms/页对；粗筛（16×16）单对 {ms*16/4096*1000:.0f} µs 量级")

    def rep(name, M):
        same = np.array([M[i, i] for i in range(n)])
        diff = np.array([M[i, j] for i in range(n) for j in range(n) if i != j])
        print(f"  {name:14s} 同页 min={same.min():.3f} p5={np.percentile(same,5):.3f} "
              f"中位={np.median(same):.3f} | 异页 p99={np.percentile(diff,99):.3f} "
              f"max={diff.max():.3f} 中位={np.median(diff):.3f} | 间距={same.min()-diff.max():+.3f}")

    print("分离度：")
    rep("平移精比", AL)
    rep("精比+pHash", FU)
    rep("粗筛16x16", CO)

    # 哈希通道的召回能力（能否作为候选筛）
    for name, ha, hb, bits in (("pHash64", pa, pb, 64), ("bhash32", ba, bb, 32)):
        d = np.bitwise_xor(ha[:, None], hb[None, :])
        d = core.popcount64(d.astype(np.uint64)) if bits == 64 else core.popcount32(d)
        same = np.array([d[i, i] for i in range(n)])
        diff = d[~np.eye(n, dtype=bool)]
        print(f"  {name} 汉明距离：同页 max={same.max()} 中位={np.median(same):.0f} | "
              f"异页 p5={np.percentile(diff,5):.0f} min={diff.min():.0f}")

    # 多页投票：模拟「一本书 8 页」的判定
    print("\n多页投票模拟（每本 8 页，K=3）：")
    for thr in (0.65, 0.70, 0.75, 0.80):
        cnt_same = 0
        cnt_diff = 0
        for i in range(n // 8 or 1):
            ii = list(range(i * 8, min(n, i * 8 + 8)))
            cnt_same += int((FU[np.ix_(ii, ii)] >= thr).sum() >= 3)
        # 用「跨书」的块当反例
        blocks = [list(range(i * 8, min(n, i * 8 + 8))) for i in range(max(1, n // 8))]
        for a_i in range(len(blocks)):
            for b_i in range(len(blocks)):
                if a_i == b_i:
                    continue
                cnt_diff += int((FU[np.ix_(blocks[a_i], blocks[b_i])] >= thr).sum() >= 3)
        print(f"  阈值 {thr:.2f}：同书块命中 {cnt_same}/{len(blocks)}，"
              f"跨书块误报 {cnt_diff}/{len(blocks)*(len(blocks)-1)}")


if __name__ == "__main__":
    main()
