# -*- coding: utf-8 -*-
"""并发正确性回归：单线程 vs 多线程扫同一份语料，结果必须**逐字节一致**。

验证三件事：

1. 两种线程数下导出的 CSV 完全一样（sha256 相等）；
2. 缓存库里没有「声明有抽样页、实际一行都没有」的残缺书；
3. 没有重复的页主键（说明多线程没把同一批页插两遍）。

用法
    python tools/probe_concurrency.py [线程数，默认 6]
前置
    先跑 tools/make_testdata.py 生成 testdata/ 语料
"""
from __future__ import annotations

import hashlib
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TESTDATA = ROOT / "testdata"
TMP = Path(tempfile.gettempdir()) / "comicdedup_probe"
TMP.mkdir(parents=True, exist_ok=True)
PY = sys.executable
NW = int(sys.argv[1]) if len(sys.argv) > 1 else 6


def run(jobs: int, tag: str):
    db, csv = TMP / f"conc_{tag}.sqlite", TMP / f"conc_{tag}.csv"
    for p in (db, csv):
        if p.exists():
            p.unlink()
    t0 = time.time()
    r = subprocess.run(
        [PY, "-u", "comic_dedup.py", "--scan", str(TESTDATA),
         "--db", str(db), "--csv", str(csv), "--jobs", str(jobs)],
        cwd=str(ROOT), capture_output=True, text=True,
        encoding="utf-8", errors="replace", timeout=3600)
    el = time.time() - t0
    out = (r.stdout or "") + (r.stderr or "")
    nw = m.group(1) if (m := re.search(r"扫描线程数：(\d+)", out)) else "?"
    scan = [l for l in out.splitlines() if "扫描完成" in l and "耗时" in l]
    res = " ".join(l.strip() for l in out.splitlines() if "重复组" in l and "可回收" in l)
    print(f"  [{tag}] 线程数={nw}  用时 {el:.1f}s")
    if scan:
        print(f"        {scan[0].strip()[:100]}")
    return db, csv, el, res


def audit(db: Path, tag: str):
    con = sqlite3.connect(db)
    nb = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    npp = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    zombie = con.execute(
        "SELECT COUNT(*) FROM books b WHERE b.sampled>0 AND "
        "(SELECT COUNT(*) FROM pages p WHERE p.book_id=b.id)<>b.sampled").fetchone()[0]
    dup = con.execute("SELECT COUNT(*) FROM (SELECT book_id,idx FROM pages "
                      "GROUP BY book_id,idx HAVING COUNT(*)>1)").fetchone()[0]
    con.close()
    print(f"  {tag}: books={nb}  pages={npp}  残缺书={zombie}  重复页键={dup}")
    return zombie, dup


def main() -> int:
    if not TESTDATA.is_dir():
        print(f"缺少语料目录 {TESTDATA}，请先运行 tools/make_testdata.py")
        return 2

    print("=== 单线程 (--jobs 1) ===")
    db1, csv1, el1, res1 = run(1, "t1")
    print(f"\n=== 多线程 (--jobs {NW}) ===")
    db2, csv2, el2, res2 = run(NW, "t6")

    print("\n=== 结果一致性 ===")
    h1 = hashlib.sha256(csv1.read_bytes()).hexdigest()
    h2 = hashlib.sha256(csv2.read_bytes()).hexdigest()
    print(f"  单线程 CSV sha256 {h1[:16]}…  {csv1.stat().st_size} 字节")
    print(f"  多线程 CSV sha256 {h2[:16]}…  {csv2.stat().st_size} 字节")

    bad = 0
    if h1 == h2:
        print("  -> CSV 逐字节一致  [OK]")
    else:
        print("  -> CSV 不一致  [FAIL]")
        bad += 1
    if res1 and res1 == res2:
        print(f"  -> 分组结论一致  [OK]  {res1}")
    else:
        print(f"  -> 分组结论不一致  [FAIL]  {res1}  vs  {res2}")
        bad += 1

    print("\n=== 缓存完整性 ===")
    z1, d1 = audit(db1, "单线程")
    z2, d2 = audit(db2, "多线程")
    for z, n_ in ((z1, "单线程"), (z2, "多线程")):
        if z:
            print(f"  -> {n_} 有 {z} 本残缺书  [FAIL]")
            bad += 1
    for d, n_ in ((d1, "单线程"), (d2, "多线程")):
        if d:
            print(f"  -> {n_} 有 {d} 个重复页键  [FAIL]")
            bad += 1
    if not (z1 or z2 or d1 or d2):
        print("  -> 无残缺书、无重复页键  [OK]")

    print(f"\n=== 提速 ===  单线程 {el1:.1f}s -> {NW} 线程 {el2:.1f}s，"
          f"加速 {el1/el2:.2f}x（含进程启动与分组的固定开销）")
    print(f"\n== 结论：{'全部通过' if not bad else f'{bad} 项失败'} ==")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
