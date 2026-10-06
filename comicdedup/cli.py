"""命令行入口与自检。

约定（沿用视频查重项目踩坑后的做法）：

* 控制台输出一律用 **ASCII 符号**（OK/FAIL/*/!），不用 ✓✗★⚠ ——
  GBK 码页编不出这些字符，重定向到文件时会直接崩。
* 输出的**编码按目的地自适应**（见 `setup_console`）：控制台保持系统码页、
  重定向到文件或管道时用 UTF-8。
* ``--noconsole`` 打包后 ``sys.stdout`` 是 ``None``，所有输出走安全 ``say()``。
"""

from __future__ import annotations

import argparse
import io
import logging
import re
import sys
import time
from pathlib import Path

import numpy as np

from . import APP_NAME, __version__, data_dir
from . import archives as A
from . import core
from .cache import FeatureCache
from .engine import (PRESETS, GroupSettings, ScanSettings, ScanWorker, export_csv,
                     group_books)

# ------------------------------------------------------------------ 输出


class _NullWriter:
    def write(self, *_a, **_k):
        return 0

    def flush(self):
        pass

    def isatty(self):
        return False


def _console_cp() -> int:
    """当前控制台的输出代码页（拿不到返回 0）。"""
    try:
        import ctypes
        return int(ctypes.windll.kernel32.GetConsoleOutputCP() or 0)
    except Exception:
        return 0


def setup_console():
    """控制台编码兜底。规则如下，目标是「中文在任何终端下都能看」：

    1. ``--noconsole`` 打包后 ``sys.stdout`` 是 ``None`` → 换成空写入器，
       否则 ``say()`` 一调用就抛异常。
    2. **输出到控制台**：保持系统给的那套编码（中文 Windows 默认 GBK），
       cmd / PowerShell 里就能正常显示中文。只有控制台本身已经是 UTF-8
       码页（65001，比如 Windows Terminal 开了「使用 UTF-8」）时才用 UTF-8。
    3. **输出被重定向**（``> 文件`` 或管道）：一律 UTF-8。GBK 文件被
       VS Code / Git Bash / Python 默认读法一读就是乱码，UTF-8 才是现在
       通用的做法；打包后的 exe 尤其需要这一条（用户很容易把 --scan 的结果
       重定向成清单文件）。

    ⚠ 不要简单粗暴地「一律改成 UTF-8」：在 chcp 936 的老 cmd 里，
    UTF-8 字节会显示成一片乱码。
    """
    if sys.stdout is None:
        sys.stdout = _NullWriter()
    if sys.stderr is None:
        sys.stderr = _NullWriter()

    is_console = False
    try:
        is_console = bool(sys.stdout.isatty())
    except Exception:
        pass
    want = "utf-8" if (not is_console or _console_cp() == 65001) else None

    for stream in (sys.stdout, sys.stderr):
        try:
            if want:
                stream.reconfigure(encoding=want, errors="replace")
            else:
                stream.reconfigure(errors="replace")
        except Exception:
            pass


def _code_haystack(fn) -> str:
    """取出一个函数「代码里出现过的标识符」，供静态检查使用。

    打包成 exe 之后磁盘上没有 ``.py``（PyInstaller 只留字节码），
    ``inspect.getsource`` 会抛 ``OSError: could not get source code``。
    这时退化成扫字节码里的名字表与常量 —— 对「有没有调用某个危险函数」
    「参数里有没有 max_items」这类检查来说，效果与读源码一样。
    """
    try:
        import inspect
        return inspect.getsource(fn)
    except Exception:
        pass
    parts: list = []
    seen: set = set()
    stack = [getattr(fn, "__code__", None)]
    while stack:
        co = stack.pop()
        if co is None or id(co) in seen:
            continue
        seen.add(id(co))
        for attr in ("co_names", "co_varnames", "co_freevars", "co_cellvars"):
            parts.extend(getattr(co, attr, ()) or ())
        for c in getattr(co, "co_consts", ()) or ():
            if hasattr(c, "co_code"):
                stack.append(c)
            elif isinstance(c, str):
                parts.append(c)
    return "\n".join(parts)


def say(msg: str = ""):
    try:
        print(msg)
    except Exception:
        try:
            print(str(msg).encode("ascii", "replace").decode("ascii"))
        except Exception:
            pass


def fmt_bytes(n) -> str:
    return core.human_size(n)


# ------------------------------------------------------------------ 扫描


def cmd_scan(a) -> int:
    root = Path(a.scan).expanduser().resolve()
    if not root.is_dir():
        say(f"[FAIL] 目录不存在：{root}")
        return 2
    s = ScanSettings.from_preset(a.preset, root=root,
                                 page_thr=a.sim, min_pages=a.min_pages,
                                 ratio_thr=a.ratio, coarse_thr=a.coarse,
                                 use_cache=not a.no_cache,
                                 do_deskew=not a.no_deskew,
                                 max_books=a.limit,
                                 threads=a.jobs)
    cache = FeatureCache(Path(a.db) if a.db else None)
    t0 = time.time()
    say(f"== {APP_NAME} v{__version__} ==")
    say(f"根目录   : {root}")
    say(f"采样预设 : {a.preset}  (锚点 {s.anchors} / 窗口 ±{s.window} / 上限 {s.max_pages} 页)")
    say(f"页级阈值 : {s.page_thr}   最少匹配页数 {s.min_pages}   命中率下限 {s.ratio_thr}")
    say(f"缓存     : {'关闭' if a.no_cache else cache.path}")
    say("")

    worker = ScanWorker(s, cache, on_log=lambda lv, m: say(("  ! " if lv != "info" else "  ") + m))
    last = [0.0]

    def prog(i, total, path, st):
        now = time.time()
        if now - last[0] < 0.5 and i != total:
            return
        last[0] = now
        pct = 100.0 * i / max(1, total)
        say(f"\r  扫描 {i}/{total} ({pct:5.1f}%)  缓存命中 {st.n_cached}  新算 {st.n_scanned}  "
            f"异常 {st.n_bad}   {Path(path).name[:40]:<40}".rstrip())

    worker.on_progress = prog
    st = worker.run()
    say("")
    if worker.errors:
        say(f"* 跳过 {len(worker.errors)} 本（前 10 条）：")
        for p, e in worker.errors[:10]:
            say(f"    - {Path(p).name}: {e[:120]}")

    say("")
    say("正在比对分组 ...")
    gs = GroupSettings(page_thr=s.page_thr, min_pages=s.min_pages, ratio_thr=s.ratio_thr,
                       coarse_thr=s.coarse_thr, pre_min=s.pre_min, threads=s.threads)
    groups, pairs, gstat = group_books(cache, gs, on_progress=lambda m: say("  " + m))
    say("")

    say(f"== 结果 ==  书 {gstat.get('books')} 本 / 页 {gstat.get('pages')} 页 / "
        f"候选书对 {gstat.get('candidates')} / 判重书对 {gstat.get('pair_hits')}")
    say(f"重复组 {len(groups)} 组，涉及 {gstat.get('dup_books', 0)} 本，"
        f"可回收 {fmt_bytes(sum(g.wasted for g in groups))}")
    for g in groups[: a.show]:
        say("")
        say(f"[组 {g.gid}] 相似度 {g.score:.3f}  共 {len(g.members)} 本  "
            f"合计 {fmt_bytes(g.total_size)}  可省 {fmt_bytes(g.wasted)}")
        for m in g.members:
            say(f"   {'*保留' if m.keep else '     '} {m.score:.3f} 匹配{m.matched:>3}页 "
                f"({m.ratio:.0%}) {m.pages:>4}页 {fmt_bytes(m.size):>10}  {Path(m.path).name}")
            say(f"            {m.path}")
    if len(groups) > a.show:
        say(f"\n...另有 {len(groups) - a.show} 组，见 CSV")

    if a.csv:
        n = export_csv(groups, Path(a.csv), root=root)
        say(f"\n[OK] 重复清单已导出：{a.csv}（{n} 行）")
    cache.close()
    say(f"\n总耗时 {time.time() - t0:.1f} 秒")
    return 0


# ------------------------------------------------------------------ 自检


def cmd_selftest(a) -> int:
    t0 = time.time()
    ok_n, bad_n, skip_n = 0, 0, 0

    def chk(cond, name, extra=""):
        nonlocal ok_n, bad_n
        if cond:
            ok_n += 1
            say(f"  [OK]   {name}" + (f"   {extra}" if extra else ""))
        else:
            bad_n += 1
            say(f"  [FAIL] {name}   {extra}")
        return cond

    def note(name, extra=""):
        """既不算通过也不算失败：该项在当前运行形态下**没法真正执行**。

        打包成 exe 后源码不在磁盘上，靠读源码做的静态检查就是空跑 ——
        空跑必须显式标成 SKIP，不能记成 OK（那等于假通过）。
        """
        nonlocal skip_n
        skip_n += 1
        say(f"  [SKIP] {name}" + (f"   {extra}" if extra else ""))

    say(f"== {APP_NAME} v{__version__} 自检 ==")
    say("\n[1] 运行环境")
    chk(sys.version_info >= (3, 9), f"Python {sys.version.split()[0]}")
    import cv2
    chk(True, f"OpenCV {cv2.__version__}")
    chk(True, f"numpy {np.__version__}")
    try:
        from PIL import Image
        chk(True, f"Pillow {Image.__version__}")
    except Exception as e:
        chk(False, "Pillow", repr(e))

    say("\n[2] 解压后端")
    be = A.available_backends()
    for k, label in (("sevenzip", "7-Zip (7z.exe)"), ("unrar", "UnRAR.exe"),
                     ("bsdtar", "bsdtar/libarchive"), ("py7zr", "py7zr 库"),
                     ("rarfile", "rarfile 库")):
        v = be.get(k)
        say(f"       {label:<22} {'OK  ' + str(v) if v else '未安装（不影响 zip）'}")
    chk(True, "zip 内置支持可用")

    say("\n[3] 图像预处理与特征")
    rng = np.random.default_rng(7)
    page = np.full((900, 640), 255, np.uint8)
    for k in range(6):
        cv2.rectangle(page, (40 + k * 90, 40), (110 + k * 90, 400), 0, 4)
    cv2.putText(page, "ABC", (80, 700), cv2.FONT_HERSHEY_SIMPLEX, 3, 0, 6)
    # 扫描版：黄纸底 + 光照渐变 + 黑边 + 噪点
    sc = page.astype(np.float32) * 0.78 + 40
    yy, xx = np.mgrid[0:900, 0:640].astype(np.float32)
    sc *= 1.0 - 0.25 * (xx / 640 + yy / 900) / 2
    sc = np.clip(sc + rng.normal(0, 5, sc.shape), 0, 255).astype(np.uint8)
    sc = cv2.copyMakeBorder(sc, 30, 30, 30, 30, cv2.BORDER_CONSTANT, value=20)

    def jpg(arr, q):
        from PIL import Image
        b = io.BytesIO()
        Image.fromarray(arr, "L").save(b, "JPEG", quality=q)
        return b.getvalue()

    fa = core.page_feature(jpg(page, 92), 0)
    fb = core.page_feature(jpg(sc, 70), 0)
    chk(fa is not None and fb is not None, "两种版本都能提特征")
    if fa and fb:
        al = core.aligned_similarity(fa.tile_arr(), fb.tile_arr())
        chk(al > 0.55, "同页（扫描版 vs 干净版）平移精比高分", f"{al:.3f}")
        chk(core.fused_page_score(al, core.sim_from_hamming(
            core.phash_hamming(fa.phash, fb.phash), 32)) > 0.5, "融合分数达标")
    # 不同页应当低分
    page2 = np.full((900, 640), 255, np.uint8)
    cv2.circle(page2, (320, 450), 260, 0, -1)
    fc = core.page_feature(jpg(page2, 92), 0)
    if fa and fc:
        al2 = core.aligned_similarity(fa.tile_arr(), fc.tile_arr())
        chk(al2 < 0.55, "不同页平移精比低分（不误报）", f"{al2:.3f}")

    say("\n[4] 采样策略")
    idx = core and _selftest_sampling()
    chk(len(idx) >= 5, f"一本 180 页的书抽到 {len(idx)} 页内页")
    chk(0 not in idx, "跳过封面（第 0 页）")
    chk(max(idx) < 180, "不越界")

    say("\n[5] 安全与隐私")
    from . import engine as E
    src = Path(__file__).resolve().parent
    pys = [p for p in sorted(src.glob("*.py")) if p.name != Path(__file__).name]
    if not pys:
        note("源码零网络调用（打包版不含 .py 源码，本项跳过）")
    else:
        # 注意：这里刻意把关键字拆开拼接，否则「检查代码本身」会被自己匹配到
        net_words = ("import " + "requests", "import " + "socket",
                     "import " + "urllib.request", "import " + "http.client",
                     "url" + "open(", "socket." + "socket", "import " + "aiohttp",
                     "import " + "ftplib", "import " + "telnetlib")
        hits = []
        for py in pys:
            txt = py.read_text(encoding="utf-8")
            for w in net_words:
                if w in txt:
                    hits.append(f"{py.name}:{w}")
        chk(not hits, "源码零网络调用（纯本地，不上传）",
            "、".join(hits) if hits else f"逐文件核验 {len(pys)} 个模块")

    # 删除函数的安全约束。读源码在打包版里不可用，_code_haystack 会自动退化成
    # 扫字节码的名字表，所以这一组检查在源码运行与 exe 里都成立。
    delhay = _code_haystack(E.delete_paths)
    bad_calls = [w for w in ("rmtree", "removedirs", "unlink")
                 if re.search(rf"\b{w}\b", delhay)]
    if re.search(r"\bremove\b", delhay):
        bad_calls.append("os.remove")
    chk(not bad_calls, "删除函数里没有永久删除调用",
        "、".join(bad_calls) if bad_calls else "")
    chk("send2trash" in delhay, "优先使用系统回收站")
    chk("max_items" in delhay, "有单次数量上限")

    say("\n[6] 缓存数据库")
    try:
        tmp = data_dir() / "selftest.sqlite"
        if tmp.exists():
            tmp.unlink()
        c = FeatureCache(tmp)
        if fa and fb:
            fb.idx = 1                      # 同一本书里页序号必须唯一（主键是 book_id+idx）
            bid = c.put_book("/t/a.zip", "archive", "zip", 123, 1.0, 10, 2, 0, core.DEFAULT_CROP)
            c.put_pages(bid, [fa, fb], buffered=False)
            hit = c.lookup("/t/a.zip", 123, 1.0, core.DEFAULT_CROP)
            chk(hit is not None and len(hit["pages"]) == 2, "写入后可命中缓存")
            chk(c.lookup("/t/a.zip", 124, 1.0, core.DEFAULT_CROP) is None, "大小变了则不命中")
            chk(c.lookup("/t/a.zip", 123, 1.0, "edge") is None, "裁剪模式变了则不命中")
        st = c.stats()
        chk(st["books"] >= 1, "统计正常", str(st["books"]) + " 本")
        c.close()
        try:
            tmp.unlink()
        except Exception:
            pass
    except Exception as e:
        chk(False, "缓存读写", repr(e))

    say("\n[7] 页级判重的可分性（合成样本）")
    _selftest_separation(chk)

    say("")
    tail = f"，跳过 {skip_n} 项（打包版不含源码）" if skip_n else ""
    say(f"== 自检结束：通过 {ok_n} 项，失败 {bad_n} 项{tail}，"
        f"耗时 {time.time()-t0:.1f} 秒 ==")
    return 1 if bad_n else 0


def _selftest_sampling():
    from .engine import sample_indices
    return sample_indices(180, 8, 2, 40)


def _selftest_separation(chk):
    """造 N 页「干净版 / 扫描版」，检查同页与异页分数的分布是否可分。"""
    import cv2
    from .engine import compute_matches
    n = 6
    clean, scan = [], []
    rng = np.random.default_rng(11)
    yy, xx = np.mgrid[0:700:1, 0:500:1].astype(np.float32)
    for k in range(n):
        p = np.full((700, 500), 255, np.uint8)
        for j in range(5):
            x0 = 30 + (j % 2) * 230
            y0 = 30 + (j // 2) * 210
            cv2.rectangle(p, (x0, y0), (x0 + 200, y0 + 180), 0, 4)
            cv2.polylines(p, [np.array([[x0 + 20 + int(rng.integers(0, 120)),
                                         y0 + 20 + int(rng.integers(0, 120))] for _ in range(4)],
                                       np.int32)], False, 0, 3)
        s = p.astype(np.float32) * 0.8 + 35
        s *= 1.0 - 0.22 * (xx / 500 + yy / 700) / 2
        s = np.clip(s + rng.normal(0, 5, s.shape), 0, 255).astype(np.uint8)
        s = cv2.copyMakeBorder(s, 26, 26, 26, 26, cv2.BORDER_CONSTANT, value=22)
        clean.append(core.page_feature(_jpg(p, 92), k))
        scan.append(core.page_feature(_jpg(s, 68), k))
    if any(f is None for f in clean + scan):
        chk(False, "分离度样本提特征失败")
        return
    TA = np.stack([f.tile_arr() for f in clean])
    TB = np.stack([f.tile_arr() for f in scan])
    PA = np.array([f.phash for f in clean], np.uint64)
    PB = np.array([f.phash for f in scan], np.uint64)
    M = compute_matches(TA, TB, PA, PB, 0.62)
    same = np.array([M[i, i] for i in range(n)])
    diff = np.array([M[i, j] for i in range(n) for j in range(n) if i != j])
    chk(same.min() > diff.max(), "同页最低分 > 异页最高分",
        f"同页 {same.min():.3f} / 异页 {diff.max():.3f}")
    chk(same.min() > 0.5, "同页最低分 > 0.5", f"{same.min():.3f}")
    chk(float(np.median(same)) > 0.7, "同页中位数 > 0.7", f"{np.median(same):.3f}")


def _jpg(arr, q):
    from PIL import Image
    b = io.BytesIO()
    Image.fromarray(arr, "L").save(b, "JPEG", quality=q)
    return b.getvalue()


# ------------------------------------------------------------------ 其它子命令


def cmd_backends(a) -> int:
    say(f"== 解压后端探测（{APP_NAME} v{__version__}）==")
    be = A.available_backends()
    for k, label, note in (
            ("sevenzip", "7-Zip 命令行 (7z.exe)", "读 zip / rar / 7z，最强"),
            ("unrar", "UnRAR.exe (WinRAR)", "读 rar"),
            ("bsdtar", "bsdtar / libarchive", "读 7z / rar（Windows 10+ 自带）"),
            ("py7zr", "py7zr（Python 库）", "读 7z，无需外部程序"),
            ("rarfile", "rarfile（Python 库）", "读 rar，需配 unrar"),
            ("rar", "Rar.exe (WinRAR)", "仅用于造测试档")):
        v = be.get(k)
        say(f"  {label:<26} {'[OK] ' + str(v) if v else '[--]  ' + note}")
    say("")
    say("  zip / cbz 由 Python 内置 zipfile 直接支持，永远可用。")
    say("  若机器上没有任何 7z/rar 后端，把 7z.exe 放到本程序同目录即可被识别。")
    return 0


def cmd_clear(a) -> int:
    cache = FeatureCache(Path(a.db) if a.db else None)
    before = cache.stats()
    say(f"清空前：{before}")
    cache.clear(only_history=a.keep_features)
    say(f"清空后：{cache.stats()}")
    say("[OK] 已清空" + ("（仅扫描记录）" if a.keep_features else "（特征与记录全部）"))
    cache.close()
    return 0


def cmd_verify(a) -> int:
    """无窗口的功能验证：用内存里造的迷你语料跑完整流程。"""
    import tempfile
    import cv2
    say(f"== {APP_NAME} v{__version__} 端到端验证（内存语料）==")
    tmp = Path(tempfile.mkdtemp(prefix="cdv_"))
    root = tmp / "lib"
    root.mkdir(parents=True)
    rng = np.random.default_rng(3)

    def make_page(seed, scan=False):
        # 用随机递归切分做分镜 —— 固定网格会让"毫无关系的两页"也版式雷同，
        # 那样测出来的只是假象（真实漫画每一页版式都不同）。
        r = np.random.default_rng(seed)
        p = np.full((660, 470), 255, np.uint8)

        def split(x0, y0, x1, y1, depth):
            w, h = x1 - x0, y1 - y0
            if depth <= 0 or w < 150 or h < 150 or r.random() < 0.25:
                cv2.rectangle(p, (x0, y0), (x1, y1), 0, int(r.integers(3, 6)))
                for _ in range(int(r.integers(4, 11))):
                    pts = np.array([[x0 + 6 + int(r.integers(0, max(8, w - 12))),
                                     y0 + 6 + int(r.integers(0, max(8, h - 12)))]
                                    for _ in range(int(r.integers(2, 6)))], np.int32)
                    cv2.polylines(p, [pts], bool(r.random() < 0.4), 0, int(r.integers(2, 4)))
                if r.random() < 0.5:
                    bx = x0 + int(r.integers(4, max(6, w - 60)))
                    by = y0 + int(r.integers(4, max(6, h - 60)))
                    cv2.rectangle(p, (bx, by), (bx + int(r.integers(30, max(40, w // 2))),
                                                by + int(r.integers(30, max(40, h // 2)))), 0, -1)
                return
            if (w > h) == (r.random() < 0.7):
                cut = x0 + int(w * r.uniform(0.3, 0.7))
                split(x0, y0, cut - 8, y1, depth - 1)
                split(cut + 8, y0, x1, y1, depth - 1)
            else:
                cut = y0 + int(h * r.uniform(0.3, 0.7))
                split(x0, y0, x1, cut - 8, depth - 1)
                split(x0, cut + 8, x1, y1, depth - 1)

        split(22, 22, 448, 638, 3)
        if not scan:
            return _jpg(p, 92)
        s = p.astype(np.float32) * 0.8 + 34
        yy, xx = np.mgrid[0:660, 0:470].astype(np.float32)
        s *= 1.0 - 0.2 * (xx / 470 + yy / 660) / 2
        s = np.clip(s + rng.normal(0, 5, s.shape), 0, 255).astype(np.uint8)
        s = cv2.copyMakeBorder(s, 24, 24, 24, 24, cv2.BORDER_CONSTANT, value=21)
        return _jpg(s, 68)

    import zipfile
    def zipit(name, pages):
        with zipfile.ZipFile(root / name, "w", zipfile.ZIP_DEFLATED) as z:
            for i, d in enumerate(pages):
                z.writestr(f"{i+1:03d}.jpg", d)

    base = [make_page(200 + i) for i in range(6)]
    zipit("A 扫描版.zip", [make_page(200 + i, True) for i in range(6)])
    zipit("A DL版.zip", base)
    zipit("A 改名副本.zip", base)
    zipit("B 无关.zip", [make_page(900 + i) for i in range(6)])

    cache = FeatureCache(tmp / "t.sqlite")
    s = ScanSettings.from_preset("标准", root=root)
    s.anchors, s.window, s.max_pages = 6, 0, 8
    w = ScanWorker(s, cache, on_log=lambda lv, m: None)
    st = w.run()
    say(f"扫描：{st.n_books} 本，新算 {st.n_scanned}，页特征 {st.n_pages}")
    groups, pairs, gs = group_books(cache, GroupSettings(page_thr=s.page_thr,
                                                         min_pages=s.min_pages,
                                                         ratio_thr=s.ratio_thr))
    say(f"分组：候选 {gs.get('candidates')} 对，判重 {gs.get('pair_hits')} 对，"
        f"重复组 {len(groups)} 组")
    for g in groups:
        say(f"  [组 {g.gid}] 相似度 {g.score:.3f}：" +
            " | ".join(Path(m.path).name for m in g.members))
    ok = 0
    fail = 0
    names = [set(Path(m.path).name for m in g.members) for g in groups]
    if any({"A 扫描版.zip", "A DL版.zip"} <= n for n in names):
        ok += 1
        say("  [OK]   扫描版与 DL 版被判为同一组")
    else:
        fail += 1
        say("  [FAIL] 扫描版与 DL 版未归为同一组")
    if any({"A DL版.zip", "A 改名副本.zip"} <= n for n in names):
        ok += 1
        say("  [OK]   同源改名副本被判为同一组")
    else:
        fail += 1
        say("  [FAIL] 同源改名副本未归为同一组")
    if not any("B 无关.zip" in n for n in names):
        ok += 1
        say("  [OK]   无关漫画没有被误并")
    else:
        fail += 1
        say("  [FAIL] 无关漫画被误并进重复组")
    # 导出 CSV
    csvp = tmp / "dup.csv"
    n = export_csv(groups, csvp, root=root)
    say(f"  CSV 导出行数 {n}")
    cache.close()
    say(f"== 验证结束：通过 {ok} 项，失败 {fail} 项 ==")
    say(f"（临时目录：{tmp}）")
    return 1 if fail else 0


# ------------------------------------------------------------------ 入口


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="comic_dedup",
        description=f"{APP_NAME} —— 纯本地离线漫画查重（不上传任何图片与特征）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=f"""示例：
  打开图形界面            python comic_dedup.py
  命令行扫描并导出清单    python comic_dedup.py --scan "D:\\漫画" --csv dup.csv
  先快速跑一遍            python comic_dedup.py --scan "D:\\漫画" --preset 快速
  严格模式                python comic_dedup.py --scan "D:\\漫画" --sim 0.72 --min-pages 4
  查看解压后端            python comic_dedup.py --backends
  自检                    python comic_dedup.py --selftest
  端到端验证              python comic_dedup.py --verify
  清空缓存                python comic_dedup.py --clear-cache
""")
    p.add_argument("--version", action="version", version=f"{APP_NAME} v{__version__}")
    p.add_argument("--scan", metavar="DIR", help="扫描这个根目录（命令行模式，不开界面）")
    p.add_argument("--csv", metavar="FILE", help="把重复清单导出到 CSV")
    p.add_argument("--preset", default="标准", choices=list(PRESETS),
                   help="采样预设：快速 / 标准 / 彻底 / 全页（默认 标准）")
    p.add_argument("--sim", type=float, default=0.62, help="页级相似度阈值（默认 0.62）")
    p.add_argument("--min-pages", type=int, default=3, dest="min_pages",
                   help="至少多少页匹配才算同一本（默认 3）")
    p.add_argument("--ratio", type=float, default=0.25, help="命中率下限（默认 0.25）")
    p.add_argument("--coarse", type=float, default=0.55, help="粗筛阈值（默认 0.55）")
    p.add_argument("--jobs", type=int, default=0,
                   help="工作线程数，扫描与精比共用（默认 0=自动，即 min(8, CPU 核数)）")
    p.add_argument("--limit", type=int, default=0, help="只处理前 N 本（调试用）")
    p.add_argument("--show", type=int, default=20, help="命令行里最多打印几组")
    p.add_argument("--db", metavar="FILE", help="指定缓存数据库文件位置")
    p.add_argument("--data-dir", metavar="DIR", dest="data_dir",
                   help="指定运行数据目录（缓存/缩略图/日志）")
    p.add_argument("--no-cache", action="store_true", help="忽略缓存，全部重算")
    p.add_argument("--no-deskew", action="store_true", help="关闭去斜")
    p.add_argument("--crop", default=core.DEFAULT_CROP,
                   choices=["bed", "mass", "edge", "paper"],
                   help="裁剪模式（默认 bed，实测最优）")
    p.add_argument("--selftest", action="store_true", help="运行自检并退出")
    p.add_argument("--verify", action="store_true", help="跑一遍内存端到端验证")
    p.add_argument("--backends", action="store_true", help="列出可用的解压后端")
    p.add_argument("--clear-cache", action="store_true", help="清空特征缓存")
    p.add_argument("--keep-features", action="store_true",
                   help="配合 --clear-cache：只清扫描记录，保留特征")
    p.add_argument("--log", metavar="FILE", help="把运行日志写到文件")
    return p


def main(argv=None) -> int:
    setup_console()
    args = build_parser().parse_args(argv)
    if args.data_dir:
        import os
        os.environ["COMICDEDUP_DATA_DIR"] = args.data_dir
    handlers = [logging.StreamHandler(sys.stdout)]
    if args.log:
        handlers.append(logging.FileHandler(args.log, encoding="utf-8"))
    logging.basicConfig(level=logging.INFO, handlers=handlers,
                        format="%(asctime)s %(levelname)s %(message)s", force=True)
    logging.getLogger("comicdedup").info("%s v%s 启动（纯本地，不联网）", APP_NAME, __version__)

    if args.selftest:
        return cmd_selftest(args)
    if args.verify:
        return cmd_verify(args)
    if args.backends:
        return cmd_backends(args)
    if args.clear_cache:
        return cmd_clear(args)
    if args.scan:
        return cmd_scan(args)

    # 无参数 → 图形界面
    try:
        from .qtcompat import QT_BINDING, QT_ERROR
    except Exception as e:
        say(f"[FAIL] 无法加载 Qt：{e}")
        return 3
    if not QT_BINDING:
        say("[FAIL] 没有找到 Qt 界面库，请安装任一：")
        say("    pip install PyQt5      （推荐）")
        say("    pip install PyQt6")
        say("    pip install PySide6")
        say(QT_ERROR)
        say("\n也可以直接用命令行模式：python comic_dedup.py --scan \"D:\\\\漫画\" --csv dup.csv")
        return 3
    from .gui import run_gui
    return run_gui()
