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

__version__ = "1.2.1"
APP_NAME = "漫画查重"
APP_EN = "ComicDedup"

__all__ = ["__version__", "APP_NAME", "APP_EN", "data_dir", "is_frozen", "app_root"]


def is_frozen() -> bool:
    """是否运行在 PyInstaller 打包出的 exe 里。"""
    return bool(getattr(sys, "frozen", False))


_DRIVE_MAP: dict | None = None


def drive_map() -> dict:
    """Windows 上「网络盘符 → UNC 共享」的反查表：``{'\\\\nas\\漫画': 'M:'}``。

    键统一小写，因为 Windows 的主机名/共享名不区分大小写
    （实测盘符给出的是 ``\\\\Dx4600-a887\\漫画``，而扫描路径里可能是全大写）。
    非 Windows 或查询失败时返回空表，调用方静默降级。
    """
    global _DRIVE_MAP
    if _DRIVE_MAP is not None:
        return _DRIVE_MAP
    m: dict = {}
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes
            mpr = ctypes.WinDLL("mpr")
            buf = ctypes.create_unicode_buffer(512)
            for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
                n = wintypes.DWORD(512)
                if mpr.WNetGetConnectionW(c + ":", ctypes.byref(buf),
                                          ctypes.byref(n)) == 0:
                    m[buf.value.rstrip("\\").lower()] = c + ":"
        except Exception:
            pass
    _DRIVE_MAP = m
    return m


def norm_path(p) -> str:
    """把路径收敛成**盘符形式**，让同一本书只有一种写法。

    为什么要归一：`Path.resolve()` 在 Windows 上会把映射的网络盘符展开成 UNC
    （``M:\\漫画\\a.zip`` → ``\\\\NAS\\漫画\\a.zip``），于是「用盘符扫」和
    「用命令行扫」会在缓存里留下**两套路径的同一本书**。后果不只是缓存命中不了，
    更糟的是分组时两本书都读进来 —— 同一本书自己跟自己判重（实测过：组数从
    294 暴增到 2621）。归一之后这个问题从根上消失。
    """
    s = str(p or "").replace("/", "\\")
    if s.startswith("\\\\"):
        parts = s.rstrip("\\").split("\\")
        for i in range(len(parts), 1, -1):
            drv = drive_map().get("\\".join(parts[:i]).lower())
            if drv:
                rest = parts[i:]
                s = drv + "\\" + "\\".join(rest)
                break
    if len(s) >= 2 and s[1] == ":" and s[0].isalpha():
        s = s[0].upper() + s[1:]
    return s.rstrip("\\") if len(s) > 3 else s


def app_root() -> Path:
    """程序所在目录（打包后是 exe 所在目录，源码运行时是项目根目录）。"""
    if is_frozen():
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent.parent


def data_dir() -> Path:
    """运行期数据目录（特征缓存 SQLite、封面缩略图、日志）。

    * 便携模式：程序目录下存在 `portable.txt` 时，数据放在 `程序目录\\data`；
    * 默认模式：`%LOCALAPPDATA%\\ComicDedup`。

    均可被环境变量 `COMICDEDUP_DATA_DIR` 覆盖。所有数据都在本地，不联网。
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
