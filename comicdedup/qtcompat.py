"""Qt 绑定兼容层：PyQt5 / PyQt6 / PySide6 三个绑定都能跑。

优先顺序：PyQt5 → PyQt6 → PySide6。
三个绑定的 API 几乎一致，差异集中在两处：

1. 枚举写法：PyQt5 是 `Qt.AlignCenter`（扁平），PyQt6/PySide6 是
   `Qt.AlignmentFlag.AlignCenter`（作用域枚举）。这里用 `q()` 统一取。
2. 信号：PyQt 用 `pyqtSignal`，PySide 用 `Signal`。

用法：
    from .qtcompat import QtCore, QtGui, QtWidgets, Signal, q, QT_BINDING
    btn.setAlignment(q("AlignmentFlag.AlignCenter"))     # 三种绑定通吃
    class W(QtWidgets.QWidget):
        sig = Signal(int)                                 # 三种绑定通吃
"""

from __future__ import annotations

import os

QtCore = QtGui = QtWidgets = None  # type: ignore
QT_BINDING = ""
QT_ERROR = ""

try:  # 1) PyQt5
    from PyQt5 import QtCore, QtGui, QtWidgets  # type: ignore

    QT_BINDING = "PyQt5"
except Exception as _e1:  # pragma: no cover - 取决于运行环境
    try:  # 2) PyQt6
        from PyQt6 import QtCore, QtGui, QtWidgets  # type: ignore

        QT_BINDING = "PyQt6"
    except Exception as _e2:  # pragma: no cover
        try:  # 3) PySide6（官方绑定，随 pip install pyside6 安装）
            from PySide6 import QtCore, QtGui, QtWidgets  # type: ignore

            QT_BINDING = "PySide6"
        except Exception as _e3:  # pragma: no cover
            QT_ERROR = (
                "没有找到任何 Qt 绑定。请任选其一安装：\n"
                "    pip install PyQt5\n"
                "    pip install PyQt6\n"
                "    pip install PySide6\n"
                f"原始错误：PyQt5={_e1!r} / PyQt6={_e2!r} / PySide6={_e3!r}"
            )


def q(name: str):
    """按名字取 Qt 常量，兼容作用域枚举（PyQt6/PySide6）与扁平枚举（PyQt5）。

    >>> q("AlignmentFlag.AlignCenter")
    >>> q("ItemDataRole.UserRole")
    >>> q("Orientation.Horizontal")
    """
    if QtCore is None:
        raise RuntimeError(QT_ERROR or "Qt 绑定不可用")
    obj = QtCore.Qt
    parts = name.split(".")
    for p in parts:
        obj = getattr(obj, p, None)
        if obj is None:
            return getattr(QtCore.Qt, parts[-1])  # 退回扁平写法
    return obj


def enum(obj, name: str):
    """在任意类上按名字取枚举，兼容作用域枚举与扁平枚举。

    比 ``q()`` 更通用 —— ``q()`` 只在 ``QtCore.Qt`` 里找，而像
    ``QMessageBox.StandardButton.Yes``、``QAbstractItemView.SelectionMode``、
    ``QImage.Format`` 这些是定义在各自类里的：

        enum(QtWidgets.QMessageBox, "StandardButton.Yes")
        enum(QtWidgets.QAbstractItemView, "SelectionMode.ExtendedSelection")
        enum(QtGui.QImage, "Format.Format_RGB888")
    """
    parts = name.split(".")
    cur = obj
    for p in parts:
        nxt = getattr(cur, p, None)
        if nxt is None:
            return getattr(obj, parts[-1])       # 退回扁平写法
        cur = nxt
    return cur


if QT_BINDING == "PySide6":
    Signal = QtCore.Signal  # type: ignore
    Slot = QtCore.Slot  # type: ignore
else:
    Signal = QtCore.pyqtSignal  # type: ignore
    Slot = QtCore.pyqtSlot  # type: ignore


def exec_app(app) -> int:
    """PyQt5 是 exec_()，PyQt6/PySide6 是 exec()。"""
    fn = getattr(app, "exec", None) or getattr(app, "exec_")
    return int(fn())


# ------------------------------------------------------------------ 字体

# 常见中文字体文件（按「好看 → 兜底」排序）。注册字体文件比依赖系统的
# fontconfig / DirectWrite 更可靠 —— 图形界面在极简环境（无桌面会话、
# 离屏渲染、Windows Server 未装字体包）里会拿不到任何字族，
# 那时候所有中文都会渲染成空心方块（tofu）。
CJK_FONT_FILES = (
    r"C:\Windows\Fonts\msyh.ttc",       # 微软雅黑
    r"C:\Windows\Fonts\msyhbd.ttc",     # 微软雅黑 粗体
    r"C:\Windows\Fonts\Deng.ttf",       # 等线
    r"C:\Windows\Fonts\simhei.ttf",     # 黑体
    r"C:\Windows\Fonts\simsun.ttc",     # 宋体
)

_FONT_FAMILY = ""   # 记住最终选中的字族名，供调用方设置 app 字体


def load_cjk_fonts(force: bool = False) -> str:
    """确保中文字体能正常渲染，返回选中的字族名（失败返回空串）。

    默认**只在系统一个字族都没有时**才显式注册字体文件 —— 正常桌面环境
    下 Qt 自己能找到微软雅黑，这时不做任何多余动作（避免干扰用户的字体偏好）。
    离屏渲染、CI、精简系统上才会走到注册分支。

    用法（要在 QApplication 之后调用）::

        app = QtWidgets.QApplication([])
        if load_cjk_fonts():
            app.setFont(QtGui.QFont(_FONT_FAMILY, app.font().pointSize()))
    """
    global _FONT_FAMILY
    if QtGui is None:
        return ""
    try:
        db = QtGui.QFontDatabase
        fams = list(db.families())
        if not force:
            have = [f for f in fams if any(
                k in f for k in ("YaHei", "SimSun", "SimHei", "DengXian", "Kai",
                                 "Ming", "Song", "Hei", "雅黑", "宋", "黑"))]
            if have:
                _FONT_FAMILY = have[0]
                return _FONT_FAMILY
        for path in CJK_FONT_FILES:
            if not os.path.exists(path):
                continue
            fid = db.addApplicationFont(path)
            if fid == -1:
                continue
            for fam in db.applicationFontFamilies(fid):
                if any(k in fam for k in ("YaHei", "DengXian", "SimHei", "SimSun")):
                    _FONT_FAMILY = fam
                    return fam
            got = list(db.applicationFontFamilies(fid))
            if got:
                _FONT_FAMILY = got[0]
                return _FONT_FAMILY
    except Exception:
        return ""
    return ""
