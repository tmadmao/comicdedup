# -*- coding: utf-8 -*-
"""多线程下的「暂停 / 恢复 / 停止」行为回归。

并发化之后最容易出问题的不是结果，而是**控制**：暂停还灵不灵？停止会不会卡住？
停止后缓存里会不会留下一堆「有记录、零页」的残缺书？这个脚本把这三件事都测一遍。

用法
    python tools/probe_pause.py [线程数，默认 4]
前置
    先跑 tools/make_testdata.py 生成 testdata/ 语料
"""
from __future__ import annotations

import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from comicdedup.cache import FeatureCache                              # noqa: E402
from comicdedup.engine import ScanSettings, ScanWorker                 # noqa: E402

TESTDATA = ROOT / "testdata"
TMP = Path(tempfile.gettempdir()) / "comicdedup_probe"
TMP.mkdir(parents=True, exist_ok=True)
NW = int(sys.argv[1]) if len(sys.argv) > 1 else 4


def fresh(name: str) -> Path:
    p = TMP / name
    for suf in ("", "-wal", "-shm"):
        f = Path(str(p) + suf)
        if f.exists():
            f.unlink()
    return p


def audit(db: Path) -> tuple:
    con = sqlite3.connect(db)
    nb = con.execute("SELECT COUNT(*) FROM books").fetchone()[0]
    npp = con.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
    zombie = con.execute(
        "SELECT COUNT(*) FROM books b WHERE b.sampled>0 AND "
        "(SELECT COUNT(*) FROM pages p WHERE p.book_id=b.id)<>b.sampled").fetchone()[0]
    con.close()
    return nb, npp, zombie


def start(db: Path):
    cache = FeatureCache(db)
    worker = ScanWorker(ScanSettings(root=TESTDATA, preset="标准", threads=NW), cache)
    box: dict = {}

    def go():
        box["st"] = worker.run()

    th = threading.Thread(target=go, daemon=True)
    th.start()
    return cache, worker, th, box


def main() -> int:
    if not TESTDATA.is_dir():
        print(f"缺少语料目录 {TESTDATA}，请先运行 tools/make_testdata.py")
        return 2
    bad = 0

    # ---------------- 停止 ----------------
    print(f"=== 测试 1：扫描中途「停止」（{NW} 线程）===")
    cache, worker, th, box = start(fresh("pause_stop.sqlite"))
    t0 = time.time()
    time.sleep(1.0)
    worker.stop()
    th.join(timeout=30)
    el = time.time() - t0
    if th.is_alive():
        print("  [FAIL] 停止后 30 秒仍未返回（卡住）")
        bad += 1
    else:
        st = box.get("st")
        print(f"  停止后 {el:.1f}s 内返回  [OK]")
        if st:
            # 注意：坏档只记在 n_bad 里，既不算「新算」也不算「未处理」
            tot = st.n_scanned + st.n_cached + st.n_bad + st.skipped
            print(f"    新算 {st.n_scanned} / 命中 {st.n_cached} / 坏档 {st.n_bad} / "
                  f"未处理 {st.skipped}")
            if tot == st.n_books:
                print(f"    计数自洽（{tot} = 总数 {st.n_books}）  [OK]")
            else:
                print(f"    [FAIL] 计数不自洽：{tot} != {st.n_books}")
                bad += 1
    nb, npp, zombie = audit(TMP / "pause_stop.sqlite")
    print(f"    缓存：books={nb} pages={npp} 残缺书={zombie}")
    if zombie:
        print("    [FAIL] 停止后留下了「有记录、零页」的残缺书")
        bad += 1
    else:
        print("    无残缺书（说明 run() 的 finally 落盘生效）  [OK]")
    cache.close()

    # ---------------- 暂停 / 恢复 ----------------
    print(f"\n=== 测试 2：扫描中途「暂停」再「恢复」（{NW} 线程）===")
    cache, worker, th, box = start(fresh("pause_resume.sqlite"))
    time.sleep(0.6)
    worker.pause()
    time.sleep(0.4)                       # 给在途任务收尾的时间
    a = worker.snapshot()
    n1 = a.n_scanned + a.n_cached
    time.sleep(1.2)
    b = worker.snapshot()
    n2 = b.n_scanned + b.n_cached
    print(f"    暂停后已处理 {n1} 本；等 1.2 秒后仍是 {n2} 本")
    if n2 == n1:
        print("    暂停期间零推进  [OK]")
    else:
        print(f"    [FAIL] 暂停期间又处理了 {n2 - n1} 本")
        bad += 1
    worker.resume()
    th.join(timeout=120)
    if th.is_alive():
        print("    [FAIL] 恢复后未能跑完")
        bad += 1
    else:
        st = box["st"]
        print(f"    恢复后跑完：新算 {st.n_scanned} / 命中 {st.n_cached} / "
              f"未处理 {st.skipped} / 共 {st.n_books} 本")
        if st.skipped == 0:
            print("    全部处理完  [OK]")
        else:
            print(f"    [FAIL] 还有 {st.skipped} 本没处理")
            bad += 1
    nb, npp, zombie = audit(TMP / "pause_resume.sqlite")
    print(f"    缓存：books={nb} pages={npp} 残缺书={zombie}")
    if zombie:
        print("    [FAIL] 有残缺书")
        bad += 1
    cache.close()

    print(f"\n== 结论：{'全部通过' if not bad else f'{bad} 项失败'} ==")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
