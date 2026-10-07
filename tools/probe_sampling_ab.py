"""块采样 vs 稀疏采样 的对照实验：验证「页数差大」的书对在新采样下能配上。

对给定的几对书，用两种采样方案分别抽取特征、跑同一套 LIS 判据，对比成链页数。

用法::

    # 1) 直接给（书名|A 路径|B 路径|A 页数|B 页数，可给多组）
    python tools/probe_sampling_ab.py --pair "甲的扫描版|M:\\甲(扫描).zip|M:\\甲[DL版].zip|170|242"

    # 2) 从本地 JSON 读（推荐：书多的时候好维护）
    python tools/probe_sampling_ab.py --pairs-file tools/_pairs.json

JSON 格式::

    [{"name": "甲", "a": "M:\\\\甲(扫描).zip", "b": "M:\\\\甲[DL版].zip",
      "pages_a": 170, "pages_b": 242}]

⚠ **不要把真实书单写进这个脚本**。它曾经把 5 组私人书库的完整路径硬编码在里面，
跟着公开仓库一起分发（文件名本身就带汉化组 / 版本标记这类标签，是隐私）。
现在只接受外部传入，``tools/_pairs.json`` 已在 .gitignore 里。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from comicdedup import core                        # noqa: E402
from comicdedup.engine import (sample_indices, _greedy_match, _lis_chain,  # noqa: E402
                               compute_matches)
from comicdedup.archives import open_book           # noqa: E402


def load_pages(path: str, idxs):
    """从压缩包/文件夹里抽指定页，返回 (tiles, phs, actual_idxs)。"""
    tiles, phs, got = [], [], []
    try:
        p = Path(path)
        if p.is_dir():
            from comicdedup import archives as A
            files = sorted([f for f in p.iterdir()
                            if f.is_file() and f.suffix.lower() in A.IMG_EXT],
                           key=lambda f: A._natural_key(f.name))
            for i in idxs:
                if 0 <= i < len(files):
                    data = files[i].read_bytes()
                    f = core.page_feature(data, i, do_deskew=True)
                    if f and not f.blank:
                        tiles.append(f.tile_arr())
                        phs.append(f.phash)
                        got.append(i)
        else:
            be = open_book(p)
            try:
                names = [n for (n, _s) in be.list_images()]
                for i in idxs:
                    if 0 <= i < len(names):
                        data = be.read(names[i])
                        f = core.page_feature(data, i, do_deskew=True)
                        if f and not f.blank:
                            tiles.append(f.tile_arr())
                            phs.append(f.phash)
                            got.append(i)
            finally:
                try:
                    be.close()
                except Exception:
                    pass
    except Exception as e:
        print(f"    读取失败 {path}: {e}")
    if not tiles:
        return None, None, []
    return (np.stack(tiles), np.array(phs, np.uint64), got)


def parse_pairs(args) -> list:
    out = []
    if args.pairs_file:
        fp = Path(args.pairs_file)
        if not fp.exists():
            print(f"[FAIL] 找不到书单文件 {fp}")
            return []
        for d in json.loads(fp.read_text(encoding="utf-8")):
            out.append((d["name"], d["a"], d["b"], int(d["pages_a"]), int(d["pages_b"])))
    for spec in args.pair or []:
        parts = spec.split("|")
        if len(parts) != 5:
            print(f"[FAIL] --pair 需要 5 段（书名|A|B|页数A|页数B），给了 {len(parts)} 段：{spec}")
            return []
        name, pa, pb, na, nb = parts
        out.append((name, pa, pb, int(na), int(nb)))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="块采样 vs 稀疏采样 对照")
    ap.add_argument("--pair", action="append",
                    help="书名|路径A|路径B|页数A|页数B（可重复）")
    ap.add_argument("--pairs-file", dest="pairs_file", default="",
                    help="从 JSON 读书单（见脚本头部说明）")
    args = ap.parse_args()

    pairs = parse_pairs(args)
    if not pairs:
        print("没有可用的书对。用法：")
        print('  python tools/probe_sampling_ab.py --pair "书名|M:\\A.zip|M:\\B.zip|170|242"')
        print("  python tools/probe_sampling_ab.py --pairs-file tools/_pairs.json")
        return 2

    schemes = {
        "旧 8锚点×±2": (8, 2, 40),
        "新 4块×13页": (4, 6, 52),
    }
    for name, pa, pb, na, nb in pairs:
        print("=" * 70)
        print(f"{name}  ({na} v {nb} 页，页数差 {abs(na - nb) / max(na, nb) * 100:.0f}%)")
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
