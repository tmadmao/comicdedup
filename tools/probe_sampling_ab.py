"""块采样 vs 稀疏采样 的对照实验：验证「页数差大」的书对在新采样下能配上。

对指定的几对「名字该重复但被漏判」的书，用两种采样方案分别抽取特征，
跑同一套 LIS 判据，对比成链页数。

用法：
    python tools/probe_sampling_ab.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from comicdedup import core                        # noqa: E402
from comicdedup.engine import (sample_indices, _greedy_match, _lis_chain,  # noqa: E402
                               compute_matches)
from comicdedup.archives import open_book           # noqa: E402


def load_pages(path: str, idxs):
    """从压缩包里抽指定页，返回 (tiles, phs, actual_idxs)。"""
    tiles, phs, got = [], [], []
    try:
        be = open_book(Path(path))
        names = [n for (n, _s) in be.list_images()]
        for i in idxs:
            if 0 <= i < len(names):
                data = be.read(names[i])
                f = core.page_feature(data, i, do_deskew=True)
                if f and not f.blank:
                    tiles.append(f.tile_arr())
                    phs.append(f.phash)
                    got.append(i)
    except Exception as e:
        print(f"    读取失败 {path}: {e}")
    if not tiles:
        return None, None, []
    return (np.stack(tiles), np.array(phs, np.uint64), got)


def main() -> int:
    # 私人书库的书对不写进仓库：由命令行 / 本地 JSON 传入
    pairs = []
    schemes = {
        "旧 8锚点×±2": (8, 2, 40),
        "新 4块×13页": (4, 6, 52),
    }
    for name, pa, pb, na, nb in pairs:
        print("=" * 70)
        print(f"{name}  ({na} v {nb} 页，页数差 {abs(na-nb)/max(na,nb)*100:.0f}%)")
        for sname, (an, w, mp) in schemes.items():
            ia = sample_indices(na, an, w, mp)
            ib = sample_indices(nb, an, w, mp)
            TA, PA, ga = load_pages(pa, ia)
            TB, PB, gb = load_pages(pb, ib)
            if TA is None or TB is None:
                print(f"  {sname:12} 读图失败")
                continue
            M = compute_matches(TA, TB, PA, PB, 0.62)
            ms = _greedy_match(M, 0.62)
            chain = _lis_chain(ms)
            print(f"  {sname:12} 抽 {len(ga)}v{len(gb)} 页 → 命中 {len(ms)} 页 / "
                  f"成链 {chain} 页  (判重需 ≥3)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
