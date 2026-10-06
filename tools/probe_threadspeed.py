# -*- coding: utf-8 -*-
"""特征提取吞吐与线程加速比测量（四组对照）。

     A  单线程 + OpenCV 默认线程   ← v1.0.0 的真实行为（OpenCV 内部会自己并行）
     B  单线程 + OpenCV 线程=1     ← 隔离出「OpenCV 内部并行」到底贡献了多少
     C  N 线程 + OpenCV 线程=1     ← v1.1.0 的行为
     D  M 线程 + OpenCV 线程=1

之所以要测 B：如果 OpenCV 本来就在偷偷吃多核，那「多线程提速」的数字就是虚高的。
（实测 B/A ≈ 0.93，也就是 OpenCV 的内部并行在这里几乎没收益 —— 算子都作用在
1024px 的小图上，调度开销把收益吃掉了。所以现在主动把它设为 1，改在页级并行。）

用法
    python tools/probe_threadspeed.py [页数，默认 240]
前置
    先跑 tools/make_testdata.py 生成 testdata/ 语料
"""
from __future__ import annotations

import os
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import cv2                                                            # noqa: E402

from comicdedup import archives as A                                  # noqa: E402
from comicdedup import core                                           # noqa: E402

TESTDATA = ROOT / "testdata"
N = int(sys.argv[1]) if len(sys.argv) > 1 else 240


def collect_samples(limit: int = 48) -> list:
    """从语料里抓若干张不同的真实内页。"""
    imgs = []
    for b in A.scan_books(TESTDATA):
        if len(imgs) >= limit:
            break
        if b.kind != "archive":
            continue
        try:
            bk = A.open_book(b.path)
            for name in [m[0] for m in bk.list_images()][:4]:
                d = bk.read(name)
                if d:
                    imgs.append(d)
            bk.close()
        except Exception:
            continue
    return imgs


def bench(imgs: list, tasks: list, nw: int, cv_threads: int, tag: str) -> float:
    try:
        cv2.setNumThreads(cv_threads)
    except Exception:
        pass
    core.page_feature(imgs[0], 0)                      # 预热（首张触发 lazy 初始化）
    t0 = time.time()
    if nw == 1:
        for d in tasks:
            core.page_feature(d, 0)
    else:
        with ThreadPoolExecutor(max_workers=nw) as ex:
            list(ex.map(lambda d: core.page_feature(d, 0), tasks))
    el = time.time() - t0
    print(f"  {tag:32s} {el:7.2f}s  {el/len(tasks)*1000:6.1f} ms/页  "
          f"{len(tasks)/el:6.1f} 页/秒")
    return el


def main() -> int:
    if not TESTDATA.is_dir():
        print(f"缺少语料目录 {TESTDATA}，请先运行 tools/make_testdata.py")
        return 2
    imgs = collect_samples()
    if not imgs:
        print("没抓到样本页")
        return 2
    tasks = [imgs[i % len(imgs)] for i in range(N)]
    cores = os.cpu_count() or 4

    print(f"样本 {len(imgs)} 张不同内页，共测 {N} 页，本机 {cores} 个逻辑核\n")
    ta = bench(imgs, tasks, 1, 0, "A 单线程 + OpenCV 默认线程")
    tb = bench(imgs, tasks, 1, 1, "B 单线程 + OpenCV 线程=1")
    tc = bench(imgs, tasks, cores, 1, f"C {cores} 线程 + OpenCV 线程=1")
    td = bench(imgs, tasks, max(2, cores * 2), 1, f"D {cores*2} 线程 + OpenCV 线程=1")

    print("\n=== 加速比 ===")
    print(f"  OpenCV 内部并行的贡献 B/A = {tb/ta:.2f}x"
          f"（接近 1 说明它本来就没帮上忙）")
    print(f"  纯页级并行 B -> C        = {tb/tc:.2f}x")
    print(f"  改造前 -> 改造后 A -> C  = {ta/tc:.2f}x")
    print(f"  再加倍线程 C -> D        = {tc/td:.2f}x（大概率开始变差：核数只有 {cores}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
