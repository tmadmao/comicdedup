# -*- coding: utf-8 -*-
"""特殊字符压力测试：外层文件名 + 压缩包内部成员名。

关注两类风险：
  1. 压缩包**自己的文件名**含 ~ # [ ] & % 空格 中日文等 —— 路径交给外部程序时是否会坏；
  2. 包**内部成员名**含非 ASCII —— archives.py 把外部程序 stdout 按 UTF-8 解码，
     而 7z/UnRAR 在中文 Windows 上通常输出 GBK，名字可能被解成乱码，
     而这个名字又被原样拿去解压，于是抽不到页。
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, r"E:\pj\manhuachachong")

import numpy as np
from PIL import Image

from comicdedup import archives as A

T = Path(tempfile.gettempdir()) / "cd_special"
if T.exists():
    shutil.rmtree(T, ignore_errors=True)
T.mkdir(parents=True)

SZ = "C:/Program Files/7-Zip-Zstandard/7z.exe"
RAR = "C:/Program Files/WinRAR/Rar.exe"

# ---- 造 3 张内容可区分的图（用像素均值当指纹，验证抽出来的字节是不是"那一页"）
imgs = {}
for i, gray in enumerate((30, 128, 220)):
    arr = np.full((200, 150), gray, np.uint8)
    arr[10:30, 10:30] = 255          # 一个小标记，避免全纯色
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="JPEG", quality=90)
    imgs[i] = buf.getvalue()

# 内部成员名：混合中日文、波浪号、井号、方括号、百分号、空格
MEMBERS = [
    "001 第一話.jpg",
    "002 #hashtag&percent%.jpg",
    "003 [brackets] 〜波ダッシュ〜 第2話.jpg",
]

# ---- 造三种格式
src = T / "_src"
src.mkdir()
for i, name in enumerate(MEMBERS):
    (src / name).write_bytes(imgs[i])

made = {}
p = T / "inner.zip"
with zipfile.ZipFile(p, "w") as z:
    for i, name in enumerate(MEMBERS):
        z.writestr(name, imgs[i])
made["zip"] = p

p = T / "inner.7z"
r = subprocess.run([SZ, "a", "-y", "-bso0", "-bsp0", str(p), *MEMBERS],
                   cwd=str(src), capture_output=True)
made["7z"] = p if r.returncode == 0 else None
if made["7z"] is None:
    print("  7z 创建失败:", (r.stderr or b"").decode("utf-8", "replace")[:200])

if Path(RAR).exists():
    p = T / "inner.rar"
    r = subprocess.run([RAR, "a", "-y", "-idq", str(p), *MEMBERS],
                       cwd=str(src), capture_output=True)
    made["rar"] = p if r.returncode == 0 else None
else:
    made["rar"] = None

print("=== 一、包内成员名含非 ASCII ===")
print(f"目标成员名: {MEMBERS}\n")
bad = 0
for fmt, path in made.items():
    if path is None or not path.exists():
        print(f"[{fmt}] 跳过（没造出来）")
        continue
    try:
        bk = A.open_book(path)
        listed = bk.list_images()
        names = [n for n, _ in listed]
        print(f"[{fmt}] 列出的成员 ({len(names)}):")
        ok_names = 0
        for n in names:
            mark = "OK " if n in MEMBERS else "坏!"
            if n in MEMBERS:
                ok_names += 1
            print(f"    {mark} {n!r}")
        # 逐个读，验证拿到的字节确实是"那一页"
        got_ok = 0
        for i, want in enumerate(MEMBERS):
            if want not in names:
                continue
            try:
                blob = bk.read(want)
                arr = np.asarray(Image.open(io.BytesIO(blob)).convert("L"))
                mean = float(arr.mean())
                expect = (30, 128, 220)[i]
                if abs(mean - expect) < 12:
                    got_ok += 1
                else:
                    print(f"    内容不符 {want!r}: 均值 {mean:.0f} 期望 {expect}")
            except Exception as e:
                print(f"    读取失败 {want!r}: {type(e).__name__}: {e}")
        bk.close()
        print(f"  -> 名字正确 {ok_names}/{len(MEMBERS)}，内容正确 {got_ok}/{len(MEMBERS)}")
        if ok_names != len(MEMBERS) or got_ok != len(MEMBERS):
            bad += 1
    except Exception as e:
        print(f"[{fmt}] 整体失败: {type(e).__name__}: {e}")
        bad += 1
    print()

# ---- 二、外层文件名含特殊字符
print("=== 二、压缩包自己的文件名含特殊字符 ===")
NASTY = [
    "テスト [漢化組] 第01巻 〜v2〜 #100%.7z",
    "漫画 第01卷 (自制扫描) [汉化组] & 『特别版』.zip",
    "名前 に 空白 と％と＃と＆.zip",
]
for i, nm in enumerate(NASTY):
    try:
        # 复制一份 7z / zip 内容到脏名字下
        if nm.endswith(".zip"):
            base = made["zip"]
        else:
            base = made["7z"] or made["zip"]
        if base is None:
            continue
        dst = T / nm
        shutil.copy2(base, dst)
        bk = A.open_book(dst)
        listed = bk.list_images()
        names = [n for n, _ in listed]
        first = names[0] if names else None
        blob = bk.read(first) if first else None
        ok = blob is not None and len(blob) > 100
        bk.close()
        print(f"  {'OK ' if ok else '坏!'} {nm}")
        print(f"       列出 {len(names)} 个成员，读取第一个: {'成功' if ok else '失败'}")
        if not ok:
            bad += 1
    except Exception as e:
        print(f"  坏! {nm}  -> {type(e).__name__}: {e}")
        bad += 1

print(f"\n== 结论：{'全部通过' if not bad else f'{bad} 项有问题'} ==")
print(f"（临时目录 {T} 保留，便于复查）")
