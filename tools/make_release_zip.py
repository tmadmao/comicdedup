"""把 onedir 打包产物压成发布用的 zip。

为什么要 onedir + zip 这一套，而不是单文件 exe：
  * 单文件（``--onefile``）每次启动都要把 ~260MB 依赖解压到临时目录，
    双击后要等 5~10 秒；onedir 是解压好的文件夹，启动是瞬时的。
  * 单文件的自解压行为在个别杀软（实测 360 的启发式 `HEUR/QVM*.Malware.Gen`）
    眼里与"释放载荷的木马"高度相似，容易被误报；
    onedir 不含自解压，误报面小得多。

用法::

    python tools/make_release_zip.py                 # 打包默认目录
    python tools/make_release_zip.py --name ComicDedupTool
"""

from __future__ import annotations

import argparse
import sys
import time
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", default="ComicDedupTool")
    ap.add_argument("--version", default="")
    args = ap.parse_args()

    folder = ROOT / "dist" / args.name
    if not folder.is_dir():
        print(f"找不到目录 {folder}")
        print("先打包：双击 打包exe.bat，或执行 "
              "python -m PyInstaller --onedir --noconsole ... comic_dedup.py")
        return 1

    ver = args.version
    if not ver:
        try:
            sys.path.insert(0, str(ROOT))
            from comicdedup import __version__
            ver = f"v{__version__}"
        except Exception:
            ver = "v0.0.0"

    out = ROOT / "dist" / f"{args.name}-{ver}-win64.zip"
    files = [p for p in folder.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    print(f"压缩 {folder.name}/ → {out.name}")
    print(f"  {len(files)} 个文件，原始 {total / 1048576:.1f} MB")

    t0 = time.time()
    # ZIP_DEFLATED 对 dll/pyd 压缩比一般，但能省下不少；大文件不重复压缩已压缩过的内容
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for p in sorted(files):
            z.write(p, arcname=str(Path(args.name) / p.relative_to(folder)))
    print(f"  完成：{out}（{out.stat().st_size / 1048576:.1f} MB，"
          f"{time.time() - t0:.1f} 秒）")
    print(f"\n提示：用户解压后得到 {args.name}\\ 文件夹，双击里面的 "
          f"{args.name}.exe 即可运行。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
