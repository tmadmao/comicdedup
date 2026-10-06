#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""漫画查重（ComicDedup）—— 启动入口。

    python comic_dedup.py                    # 打开图形界面
    python comic_dedup.py --scan "D:\\漫画"   # 命令行扫描
    python comic_dedup.py --selftest         # 自检
    python comic_dedup.py --help             # 全部参数

本程序**纯本地离线运行**：不联网、不上传任何图片或特征数据、不做 OCR，
删除操作一律需要人工勾选并二次确认，且只移到回收站。
"""

import os
import sys
from pathlib import Path

# 允许直接双击/直接运行本文件（把项目根目录加进搜索路径）
sys.path.insert(0, str(Path(__file__).resolve().parent))


def _crash_report(exc: BaseException):
    """顶层异常兜底：打包成无控制台的 exe 时，双击闪退是没有任何提示的。"""
    import traceback
    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        from comicdedup import data_dir
        p = data_dir() / "崩溃日志.txt"
        p.write_text(tb, encoding="utf-8")
    except Exception:
        p = None
    try:
        sys.stderr.write(tb)
    except Exception:
        pass
    try:
        import tkinter.messagebox as mb
        mb.showerror("漫画查重 - 启动失败",
                     f"程序启动时出错，详情已写入：\n{p}\n\n{tb[-1500:]}")
    except Exception:
        pass


def main() -> int:
    try:
        from comicdedup.cli import main as cli_main
        return cli_main()
    except SystemExit:
        raise
    except BaseException as e:  # noqa: BLE001
        _crash_report(e)
        return 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONUTF8", "1")
    sys.exit(main())
