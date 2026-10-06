"""用「文件名」造一个查重的真值代理，用来量工具的召回率。

思路：这批漫画单行本的命名高度规范 ——

    [作者] 标题 [汉化组] [版本标记] [DL版].zip

把方括号/圆括号里的内容（作者、汉化组、版本标记）全部剥掉，剩下的就是**作品名**；
再抽出卷号。同一个「作品名 + 卷号」下的多本，几乎必然是同一本漫画的不同版本
（不同汉化、扫描版 vs DL 版、无修 vs 有修），也就是**应该被判重**的。

于是：
  * 召回率 = 「名字判定该判重」的书对里，工具真正判重了的比例（下界，因为
    标题被翻译成不同语言时名字对不上，会低估）。
  * 误报线索 = 工具判重、但名字完全不沾边的组（未必是误报，需要人看一眼）。

用法：
    python tools/probe_name_truth.py --db <库> --groups <工具导出的分组 CSV>
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

BRACKET = re.compile(r"[\[\(（【][^\[\]\(\)（）【】]*[\]\)）】]")
VOL_PATS = [
    (re.compile(r"第\s*(\d+)\s*[卷巻册]"), 1),
    (re.compile(r"[Vv]ol(?:ume)?\.?\s*(\d+)"), 1),
    (re.compile(r"\bCh(?:apter)?\.?\s*(\d+)"), 1),
    (re.compile(r"[_\-\s](\d{1,3})\s*$"), 1),
]
PART_KW = ("前編", "後編", "前篇", "后篇", "上卷", "中卷", "下卷", "上册", "下册")


def norm_title(fn: str) -> tuple:
    """文件名 → (作品名归一化, 卷号)。"""
    s = os.path.splitext(fn)[0]
    s = BRACKET.sub(" ", s)
    s = s.replace("_", " ").replace("　", " ")
    s = re.sub(r"\s+", " ", s).strip()
    vol = ""
    for kw in PART_KW:
        if kw in s:
            vol = kw
            s = s.replace(kw, " ")
            break
    if not vol:
        for pat, _g in VOL_PATS:
            m = pat.search(s)
            if m:
                vol = m.group(1).lstrip("0") or "0"
                s = s[:m.start()] + " " + s[m.end():]
                break
    s = re.sub(r"[\s\-–—~〜+＋、,，。.:：!！?？'\"]+", " ", s).strip().lower()
    return s, vol


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=str(Path(os.environ["LOCALAPPDATA"]) /
                                        "ComicDedup" / "comics.sqlite"))
    ap.add_argument("--groups", required=True, help="工具导出的分组 CSV（--csv）")
    ap.add_argument("--show", type=int, default=12)
    args = ap.parse_args()

    import sqlite3
    con = sqlite3.connect(f"file:{Path(args.db).as_posix()}?mode=ro", uri=True)
    books = [r[0] for r in con.execute("SELECT path FROM books WHERE status='ok'")]

    key_of: dict = {}
    members: dict = defaultdict(list)
    for p in books:
        k = norm_title(os.path.basename(p))
        key_of[p] = k
        members[k].append(p)

    suspect = {k: v for k, v in members.items() if len(v) >= 2 and k[0]}
    suspect_pairs = set()
    for k, v in suspect.items():
        for i in range(len(v)):
            for j in range(i + 1, len(v)):
                suspect_pairs.add(frozenset((v[i], v[j])))
    print(f"书 {len(books)} 本 → 归一化后 {len(members)} 个「作品名+卷号」")
    print(f"其中 ≥2 本的：{len(suspect)} 组，涉及 {sum(len(v) for v in suspect.values())} 本，"
          f"共 {len(suspect_pairs)} 个「应当判重」的书对")

    # ---- 读工具的分组
    tool_groups: dict = defaultdict(set)
    with open(args.groups, encoding="utf-8-sig", newline="") as f:
        for row in csv.DictReader(f):
            tool_groups[row["组号"]].add(row["完整路径"])
    tool_pairs = set()
    for gid, paths in tool_groups.items():
        pl = sorted(paths)
        for i in range(len(pl)):
            for j in range(i + 1, len(pl)):
                tool_pairs.add(frozenset((pl[i], pl[j])))
    print(f"工具输出：{len(tool_groups)} 组 / {len(tool_pairs)} 个判重书对")

    hit = suspect_pairs & tool_pairs
    miss = suspect_pairs - tool_pairs
    print(f"\n== 召回（以名字真值为准）==")
    print(f"  抓到 {len(hit)} / {len(suspect_pairs)} = {len(hit) / max(len(suspect_pairs), 1) * 100:.1f}%")
    print(f"  漏掉 {len(miss)} 对")

    # 漏掉的对，按「作品组」聚合更可读
    miss_by_key: dict = defaultdict(list)
    for pr in miss:
        a, b = sorted(pr)
        miss_by_key[key_of[a]].append((a, b))
    print(f"  漏掉涉及 {len(miss_by_key)} 个作品组；前 {args.show} 个：")
    for k, ps in list(miss_by_key.items())[:args.show]:
        print(f"    [{k[0]}|卷{k[1]}]")
        for a, b in ps[:4]:
            print(f"        - {os.path.basename(a)}")
            print(f"          {os.path.basename(b)}")

    # 工具判重但没有名字支持的
    orphan = tool_pairs - suspect_pairs
    ok = tool_pairs & suspect_pairs
    print(f"\n== 工具判重书对的名字支持情况 ==")
    print(f"  有名字支持 {len(ok)} 对 / 没有 {len(orphan)} 对 "
          f"（前者占 {len(ok) / max(len(tool_pairs), 1) * 100:.1f}%）")
    shown = 0
    for pr in sorted(orphan, key=lambda x: sorted(x)[0]):
        a, b = sorted(pr)
        print(f"    ? {os.path.basename(a)}")
        print(f"      {os.path.basename(b)}")
        shown += 1
        if shown >= args.show:
            break
    con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
