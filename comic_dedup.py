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
    p = None
    try:
        from comicdedup import data_dir
        p = data_dir() / "崩溃日志.txt"
        p.write_text(tb, encoding="utf-8")
    except Exception:
        pass
    try:
        sys.stderr.write(tb)
    except Exception:
        pass

    # 弹窗走系统 API，不依赖任何 GUI 库：
    #   * 用 tkinter 会白白多带一整套 Tk（打包体积 +10MB 以上），
    #     而且在已经初始化了 Qt 的进程里再起一个 Tk 解释器很容易卡住
    #     （本机实测过 Tk 模态框把事件循环堵死的故障）；
    #   * 用 Qt 也不行 —— 崩溃可能发生在 Qt 装起来之前。
    msg = f"程序启动时出错，详情已写入：\n{p}\n\n{tb[-1500:]}"
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, msg, "漫画查重 - 启动失败", 0x10)
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
