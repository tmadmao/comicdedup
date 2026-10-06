"""漫画查重（ComicDedup）—— 纯本地离线运行的漫画重复检测工具。

设计要点
--------
* 全程离线：不导入任何网络库，不上传图片与特征数据。
* 一个压缩包（zip/rar/7z）或一个图片文件夹 = 一本单行本。
* 压缩包内图片一律在内存中临时读取，不落地解压。
* 每个单行本抽 5~10（可调更多）张内页，提取「墨迹密度图 + pHash」特征，
  用多页集合比对判定重复，能同时识别：
    ① 同源文件（改名 / 换压缩格式 / 重新压缩）；
    ② 同一本书的自制扫描版 与 官方 DL 电子版。
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

__version__ = "1.1.0"
APP_NAME = "漫画查重"
APP_EN = "ComicDedup"

__all__ = ["__version__", "APP_NAME", "APP_EN", "data_dir", "is_frozen", "app_root"]


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


def app_root() -> Path:
    """程序所在目录（打包后是 exe 所在目录，源码运行时是项目根目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """运行期数据目录（特征缓存 SQLite、封面缩略图、日志）。

    * 便携模式：程序目录下存在 `portable.txt` 时，数据放在 `程序目录\\data`；
    * 默认模式：`%LOCALAPPDATA%\\ComicDedup`。

    均可被环境变量 `COMICDEDUP_DATA_DIR` 覆盖。所有数据都在本机，不联网。
    """
    env = os.environ.get("COMICDEDUP_DATA_DIR")
    if env:
        base = Path(env)
    elif (app_root() / "portable.txt").exists():
        base = app_root() / "data"
    else:
        root = os.environ.get("LOCALAPPDATA") or os.environ.get("APPDATA") or str(Path.home())
        base = Path(root) / "ComicDedup"
    base.mkdir(parents=True, exist_ok=True)
    return base
