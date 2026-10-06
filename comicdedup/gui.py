"""中文图形界面（PyQt5 / PyQt6 / PySide6 通用）。

界面结构
--------
    ┌─ 顶部：根目录 / 采样预设 / 开始·暂停·停止 / 阈值滑块 ──────────────┐
    ├─ 左：重复组树（每组可展开；每项带复选框 + 封面缩略图 + 元信息）      │
    ├─ 右：选中项详情（封面大图、页数、体积、相似度、匹配页列表）          │
    └─ 底部：进度条 + 状态 + 日志                                          ┘

安全约定
--------
* **程序永不自动删除**。必须人工勾选后再点「删除选中」，且会弹二次确认窗，
  窗口里逐条列出将被移动的路径与总大小。
* 删除只做「移入系统回收站 / 本地回收目录」，绝不永久删除。
"""

from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

from . import APP_NAME, __version__, data_dir
from .core import DEFAULT_CROP, human_size
from .engine import (PRESETS, PRESET_HELP, GroupSettings, Member, ScanSettings, ScanWorker,
                     delete_paths, export_csv, export_log, group_books)
from .cache import FeatureCache
from .qtcompat import QT_BINDING, Signal, Slot, enum, exec_app, q
from .qtcompat import QtCore, QtGui, QtWidgets

THUMB_W, THUMB_H = 46, 64
PREVIEW_H = 420


# 跨绑定常量（PyQt5 是扁平枚举，PyQt6/PySide6 是作用域枚举 —— 用 enum() 统一）
MB_YES = enum(QtWidgets.QMessageBox, "StandardButton.Yes")
MB_NO = enum(QtWidgets.QMessageBox, "StandardButton.No")
SEL_EXT = enum(QtWidgets.QAbstractItemView, "SelectionMode.ExtendedSelection")
IMG_RGB888 = enum(QtGui.QImage, "Format.Format_RGB888")
FRAME_HLINE = enum(QtWidgets.QFrame, "Shape.HLine")


# ================================================================== 后台线程


class ScanThread(QtCore.QThread):
    sig_progress = Signal(int, int, str, object)
    sig_log = Signal(str, str)
    sig_done = Signal(object)

    def __init__(self, settings: ScanSettings, cache: FeatureCache):
        super().__init__()
        self.settings = settings
        self.cache = cache
        self.worker: ScanWorker | None = None

    def run(self):
        self.worker = ScanWorker(self.settings, self.cache,
                                 on_progress=lambda i, t, p, st: self.sig_progress.emit(
                                     int(i), int(t), str(p), st),
                                 on_log=lambda lv, m: self.sig_log.emit(str(lv), str(m)))
        try:
            st = self.worker.run()
        except Exception as e:                     # 后台异常也要让界面知道
            self.sig_log.emit("error", f"扫描线程异常：{type(e).__name__}: {e}")
            st = None
        self.sig_done.emit(st)


class GroupThread(QtCore.QThread):
    sig_progress = Signal(str)
    sig_done = Signal(object, object, object)

    def __init__(self, cache: FeatureCache, gs: GroupSettings):
        super().__init__()
        self.cache = cache
        self.gs = gs
        self._stop = False

    def stop(self):
        self._stop = True

    def run(self):
        try:
            groups, pairs, stats = group_books(self.cache, self.gs,
                                               on_progress=lambda m: self.sig_progress.emit(str(m)),
                                               should_stop=lambda: self._stop)
        except Exception as e:
            self.sig_progress.emit(f"分组异常：{type(e).__name__}: {e}")
            groups, pairs, stats = [], [], {}
        self.sig_done.emit(groups, pairs, stats)


class DeleteThread(QtCore.QThread):
    sig_log = Signal(str, str)
    sig_done = Signal(object)

    def __init__(self, paths, recycle_dir):
        super().__init__()
        self.paths = list(paths)
        self.recycle_dir = recycle_dir

    def run(self):
        r = delete_paths(self.paths, recycle_dir=self.recycle_dir,
                         on_log=lambda lv, m: self.sig_log.emit(str(lv), str(m)))
        self.sig_done.emit(r)


# ================================================================== 主窗口


class MainWindow(QtWidgets.QMainWindow):

    def __init__(self, cache: FeatureCache | None = None):
        super().__init__()
        self.setWindowTitle(f"{APP_NAME} v{__version__} —— 纯本地漫画查重（不联网）")
        self.resize(1400, 900)
        self.cache = cache or FeatureCache()
        self.groups: list = []
        self.rows_by_group: dict = {}
        self.scan_thread: ScanThread | None = None
        self.group_thread: GroupThread | None = None
        self.del_thread: DeleteThread | None = None
        self.thumb_dir = data_dir() / "thumbs"
        self.root: Path | None = None
        self._build_ui()
        self._load_cfg()
        self._refresh_cache_info()

    # -------------------------------------------------------- 界面构建

    def _build_ui(self):
        central = QtWidgets.QWidget()
        self.setCentralWidget(central)
        outer = QtWidgets.QVBoxLayout(central)
        outer.setContentsMargins(8, 8, 8, 6)
        outer.setSpacing(6)

        outer.addWidget(self._build_top())
        splitter = QtWidgets.QSplitter()
        splitter.setOrientation(q("Orientation.Horizontal"))
        splitter.addWidget(self._build_tree())
        splitter.addWidget(self._build_detail())
        splitter.setStretchFactor(0, 3)
        splitter.setStretchFactor(1, 2)
        splitter.setSizes([880, 500])
        outer.addWidget(splitter, 1)
        outer.addWidget(self._build_bottom())

        self._make_actions()
        self.statusBar().showMessage(
            f"就绪 · Qt 绑定：{QT_BINDING} · 全部处理都在本地完成，不联网、不上传")

    # ---- 顶部
    def _build_top(self):
        box = QtWidgets.QGroupBox("① 选择目录并开始")
        grid = QtWidgets.QGridLayout(box)
        grid.setContentsMargins(10, 6, 10, 6)
        grid.setHorizontalSpacing(8)
        grid.setVerticalSpacing(6)

        self.ed_root = QtWidgets.QLineEdit()
        self.ed_root.setPlaceholderText("选择漫画根目录（会递归扫描 zip / rar / 7z / 图片文件夹）")
        btn_browse = QtWidgets.QPushButton("浏览…")
        btn_browse.setFixedWidth(80)
        btn_browse.clicked.connect(self.on_browse)
        self.btn_scan = QtWidgets.QPushButton("开始扫描")
        self.btn_scan.setFixedWidth(96)
        self.btn_scan.clicked.connect(self.on_scan)
        self.btn_pause = QtWidgets.QPushButton("暂停")
        self.btn_pause.setFixedWidth(70)
        self.btn_pause.setEnabled(False)
        self.btn_pause.clicked.connect(self.on_pause)
        self.btn_stop = QtWidgets.QPushButton("停止")
        self.btn_stop.setFixedWidth(70)
        self.btn_stop.setEnabled(False)
        self.btn_stop.clicked.connect(self.on_stop)

        grid.addWidget(QtWidgets.QLabel("漫画根目录"), 0, 0)
        grid.addWidget(self.ed_root, 0, 1, 1, 4)
        grid.addWidget(btn_browse, 0, 5)
        grid.addWidget(self.btn_scan, 0, 6)
        grid.addWidget(self.btn_pause, 0, 7)
        grid.addWidget(self.btn_stop, 0, 8)

        # 第二行：采样预设 + 判重参数
        grid.addWidget(QtWidgets.QLabel("采样预设"), 1, 0)
        self.cb_preset = QtWidgets.QComboBox()
        for k in PRESETS:
            self.cb_preset.addItem(k)
        self.cb_preset.setCurrentText("标准")
        self.cb_preset.setMaximumWidth(110)
        self.cb_preset.currentTextChanged.connect(self.on_preset)
        grid.addWidget(self.cb_preset, 1, 1)
        self.lb_preset = QtWidgets.QLabel()
        self.lb_preset.setStyleSheet("color:#666;")
        grid.addWidget(self.lb_preset, 1, 2, 1, 2)

        grid.addWidget(QtWidgets.QLabel("页级相似度阈值"), 2, 0)
        self.sl_sim = QtWidgets.QSlider(q("Orientation.Horizontal"))
        self.sl_sim.setRange(40, 95)
        self.sl_sim.setValue(62)
        self.sl_sim.setMaximumWidth(240)
        self.sl_sim.valueChanged.connect(self._on_slider)
        self.lb_sim = QtWidgets.QLabel("0.62")
        self.lb_sim.setFixedWidth(44)
        grid.addWidget(self.sl_sim, 2, 1)
        grid.addWidget(self.lb_sim, 2, 2)

        grid.addWidget(QtWidgets.QLabel("最少匹配页数"), 2, 3)
        self.sp_min = QtWidgets.QSpinBox()
        self.sp_min.setRange(2, 30)
        self.sp_min.setValue(3)
        self.sp_min.setMaximumWidth(70)
        grid.addWidget(self.sp_min, 2, 4)

        self.btn_regroup = QtWidgets.QPushButton("重新分组")
        self.btn_regroup.setToolTip("改完上面两个阈值后点这里，不用重新扫描")
        self.btn_regroup.clicked.connect(self.on_regroup)
        grid.addWidget(self.btn_regroup, 2, 5)

        grid.addWidget(QtWidgets.QLabel("命中率下限"), 2, 6)
        self.sl_ratio = QtWidgets.QSlider(q("Orientation.Horizontal"))
        self.sl_ratio.setRange(0, 90)
        self.sl_ratio.setValue(25)
        self.sl_ratio.setMaximumWidth(150)
        self.sl_ratio.valueChanged.connect(self._on_slider)
        self.lb_ratio = QtWidgets.QLabel("0.25")
        self.lb_ratio.setFixedWidth(44)
        grid.addWidget(self.sl_ratio, 2, 7)
        grid.addWidget(self.lb_ratio, 2, 8)

        # 第三行：CPU 线程数（扫描与精比两个阶段共用）
        grid.addWidget(QtWidgets.QLabel("CPU 线程数"), 3, 0)
        self.cb_jobs = QtWidgets.QComboBox()
        for label, val in (("自动", 0), ("1（单线程）", 1), ("2", 2), ("4", 4),
                           ("6", 6), ("8", 8), ("12", 12), ("16", 16)):
            self.cb_jobs.addItem(label, val)
        self.cb_jobs.setMaximumWidth(120)
        self.cb_jobs.setToolTip(
            "扫描与精比各用几个线程。\n"
            "「自动」= min(8, CPU 逻辑核数)，一般就是最合适的选择。\n"
            "漫画放在 NAS 上时，适当调高可以把网络等待重叠掉（比如核数的 1.5~2 倍）；\n"
            "放在本地磁盘时不要超过 CPU 逻辑核数太多，否则互相抢核反而更慢。")
        self.cb_jobs.currentIndexChanged.connect(self._on_jobs_hint)
        grid.addWidget(self.cb_jobs, 3, 1)
        self.lb_jobs = QtWidgets.QLabel()
        self.lb_jobs.setStyleSheet("color:#666;")
        grid.addWidget(self.lb_jobs, 3, 2, 1, 5)

        grid.setColumnStretch(4, 1)
        self.on_preset("标准")
        self._on_jobs_hint()
        return box

    # ---- 树
    def _build_tree(self):
        wrap = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(wrap)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)

        bar = QtWidgets.QHBoxLayout()
        bar.addWidget(QtWidgets.QLabel("② 重复组（勾选要删除的项）"))
        bar.addStretch(1)
        self.ed_filter = QtWidgets.QLineEdit()
        self.ed_filter.setPlaceholderText("筛选书名/路径…")
        self.ed_filter.setMaximumWidth(200)
        self.ed_filter.textChanged.connect(self._apply_filter)
        bar.addWidget(self.ed_filter)
        self.btn_expand = QtWidgets.QPushButton("全部展开")
        self.btn_collapse = QtWidgets.QPushButton("全部收起")
        bar.addWidget(self.btn_expand)
        bar.addWidget(self.btn_collapse)
        lay.addLayout(bar)

        self.tree = QtWidgets.QTreeWidget()
        self.tree.setColumnCount(6)
        self.tree.setHeaderLabels(["单行本名称", "文件大小", "图片页数", "相似度", "匹配页数", "完整路径"])
        self.tree.setRootIsDecorated(True)
        self.tree.setUniformRowHeights(False)
        self.tree.setIconSize(QtCore.QSize(THUMB_W, THUMB_H))
        self.tree.setSelectionMode(SEL_EXT)
        self.tree.setAlternatingRowColors(True)
        hdr = self.tree.header()
        hdr.setStretchLastSection(True)
        for i, w in enumerate((330, 90, 80, 70, 80)):
            self.tree.setColumnWidth(i, w)
        self.tree.itemSelectionChanged.connect(self.on_select)
        self.tree.itemChanged.connect(self._on_item_changed)
        self.btn_expand.clicked.connect(self.tree.expandAll)
        self.btn_collapse.clicked.connect(self.tree.collapseAll)
        lay.addWidget(self.tree, 1)

        self.lb_summary = QtWidgets.QLabel("还没有结果。选好目录后点「开始扫描」。")
        self.lb_summary.setStyleSheet("color:#444; padding:2px;")
        lay.addWidget(self.lb_summary)
        return wrap

    # ---- 右侧详情
    def _build_detail(self):
        box = QtWidgets.QGroupBox("③ 详情 / 操作")
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(8, 8, 8, 8)

        self.lb_title = QtWidgets.QLabel("（未选中）")
        self.lb_title.setWordWrap(True)
        self.lb_title.setStyleSheet("font-weight:bold;")
        lay.addWidget(self.lb_title)

        self.lb_meta = QtWidgets.QLabel("")
        self.lb_meta.setWordWrap(True)
        lay.addWidget(self.lb_meta)

        self.lb_cover = QtWidgets.QLabel()
        self.lb_cover.setAlignment(q("AlignmentFlag.AlignCenter"))
        self.lb_cover.setMinimumHeight(PREVIEW_H + 8)
        self.lb_cover.setStyleSheet("background:#f2f2f2; border:1px solid #ccc;")
        self.lb_cover.setText("封面预览")
        lay.addWidget(self.lb_cover)

        self.tb_pairs = QtWidgets.QTableWidget(0, 3)
        self.tb_pairs.setHorizontalHeaderLabels(["本项页号", "对方页号", "页相似度"])
        self.tb_pairs.horizontalHeader().setStretchLastSection(True)
        self.tb_pairs.verticalHeader().setVisible(False)
        self.tb_pairs.setMaximumHeight(200)
        lay.addWidget(QtWidgets.QLabel("匹配上的内页（人工核对用）："))
        lay.addWidget(self.tb_pairs)

        row = QtWidgets.QHBoxLayout()
        self.btn_open = QtWidgets.QPushButton("打开所在文件夹")
        self.btn_open.clicked.connect(self.on_open_folder)
        self.btn_cover = QtWidgets.QPushButton("查看封面大图")
        self.btn_cover.clicked.connect(self.on_open_cover)
        self.btn_keep = QtWidgets.QPushButton("设为保留项")
        self.btn_keep.clicked.connect(self.on_set_keep)
        row.addWidget(self.btn_open)
        row.addWidget(self.btn_cover)
        row.addWidget(self.btn_keep)
        lay.addLayout(row)

        lay.addWidget(self._hline())
        lay.addWidget(QtWidgets.QLabel("④ 删除（只移入回收站，绝不永久删除）"))
        row2 = QtWidgets.QHBoxLayout()
        self.btn_csv = QtWidgets.QPushButton("导出重复清单 CSV")
        self.btn_csv.clicked.connect(self.on_export)
        self.btn_clear = QtWidgets.QPushButton("清空缓存数据库")
        self.btn_clear.clicked.connect(self.on_clear)
        self.btn_del = QtWidgets.QPushButton("删除选中文件 ⚠")
        self.btn_del.setStyleSheet("color:#a00; font-weight:bold;")
        self.btn_del.clicked.connect(self.on_delete)
        row2.addWidget(self.btn_csv)
        row2.addWidget(self.btn_clear)
        row2.addWidget(self.btn_del)
        lay.addLayout(row2)

        self.ck_sel_group = QtWidgets.QCheckBox("勾选时整组一起（含保留项）")
        lay.addWidget(self.ck_sel_group)
        self.lb_cache = QtWidgets.QLabel()
        self.lb_cache.setStyleSheet("color:#666;")
        lay.addWidget(self.lb_cache)
        lay.addStretch(1)
        return box

    def _hline(self):
        ln = QtWidgets.QFrame()
        ln.setFrameShape(FRAME_HLINE)
        return ln

    # ---- 底部
    def _build_bottom(self):
        box = QtWidgets.QWidget()
        lay = QtWidgets.QVBoxLayout(box)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(4)
        self.pbar = QtWidgets.QProgressBar()
        self.pbar.setFormat("%p%  %v/%m")
        lay.addWidget(self.pbar)
        self.txt_log = QtWidgets.QPlainTextEdit()
        self.txt_log.setReadOnly(True)
        self.txt_log.setMaximumHeight(112)
        self.txt_log.setPlaceholderText("运行日志（坏档、跳过、异常都会记在这里，程序不会崩）")
        lay.addWidget(self.txt_log)
        return box

    def _make_actions(self):
        # QShortcut 在 Qt5 属于 QtWidgets，Qt6 移到了 QtGui —— 两边都兜住
        cls = getattr(QtGui, "QShortcut", None) or getattr(QtWidgets, "QShortcut", None)
        if cls is None:
            return
        for key, fn in (("Ctrl+O", self.on_browse), ("Ctrl+S", self.on_scan),
                        ("Ctrl+E", self.on_export), ("Ctrl+R", self.on_regroup)):
            sc = cls(QtGui.QKeySequence(key), self)
            sc.activated.connect(fn)

    # -------------------------------------------------------- 小工具

    def logline(self, level: str, msg: str):
        ts = time.strftime("%H:%M:%S")
        tag = {"error": "错误", "warn": "警告", "info": "信息"}.get(level, level)
        self.txt_log.appendPlainText(f"[{ts}] {tag}  {msg}")
        if level in ("error", "warn"):
            self.statusBar().showMessage(msg, 8000)

    def _on_slider(self):
        v = self.sl_sim.value() / 100.0
        self.lb_sim.setText(f"{v:.2f}")
        self.sl_sim.setToolTip(self._sim_hint(v))
        r = self.sl_ratio.value() / 100.0
        self.lb_ratio.setText(f"{r:.2f}")

    @staticmethod
    def _sim_hint(v: float) -> str:
        if v < 0.55:
            return "偏宽松：同源改名/换格式几乎必中，但可能把版式相近的不同页也算上"
        if v < 0.68:
            return "推荐区间：能同时抓住「同源变体」和「扫描版 vs 官方DL版」"
        if v < 0.80:
            return "偏严格：只认内容高度一致的页，扫描版可能漏掉一部分"
        return "很严格：基本只认同源文件，跨版本扫描/DL 容易漏"

    def on_preset(self, name: str):
        a, w, m = PRESETS.get(name, PRESETS["标准"])
        self.lb_preset.setText(PRESET_HELP.get(name, ""))
        self.lb_preset.setToolTip(PRESET_HELP.get(name, ""))

    def _refresh_cache_info(self):
        try:
            st = self.cache.stats()
            self.lb_cache.setText(
                f"缓存库：{Path(st['db']).name} ｜ 已缓存 {st['books']} 本 / "
                f"{st['sampled']} 页特征（其中异常 {st['bad']} 本）")
        except Exception:
            self.lb_cache.setText("缓存库：不可用")

    # -------------------------------------------------------- 配置记忆

    def _cfg_path(self) -> Path:
        return data_dir() / "ui.json"

    def _load_cfg(self):
        import json
        try:
            d = json.loads(self._cfg_path().read_text(encoding="utf-8"))
        except Exception:
            return
        self.ed_root.setText(d.get("root", ""))
        if d.get("preset") in PRESETS:
            self.cb_preset.setCurrentText(d["preset"])
        self.sl_sim.setValue(int(d.get("sim", 0.62) * 100))
        self.sp_min.setValue(int(d.get("min_pages", 3)))
        self.sl_ratio.setValue(int(d.get("ratio", 0.25) * 100))
        idx = self.cb_jobs.findData(int(d.get("jobs", 0)))
        self.cb_jobs.setCurrentIndex(idx if idx >= 0 else 0)
        self._on_jobs_hint()
        self._on_slider()

    def _save_cfg(self):
        import json
        try:
            self._cfg_path().write_text(json.dumps({
                "root": self.ed_root.text(), "preset": self.cb_preset.currentText(),
                "sim": self.sl_sim.value() / 100.0, "min_pages": self.sp_min.value(),
                "ratio": self.sl_ratio.value() / 100.0,
                "jobs": int(self.cb_jobs.currentData() or 0),
            }, ensure_ascii=False, indent=1), encoding="utf-8")
        except Exception:
            pass

    def closeEvent(self, ev):
        self._save_cfg()
        try:
            if self.scan_thread and self.scan_thread.isRunning():
                self.scan_thread.worker and self.scan_thread.worker.stop()
                self.scan_thread.wait(3000)
            if self.group_thread and self.group_thread.isRunning():
                self.group_thread.stop()
                self.group_thread.wait(3000)
        except Exception:
            pass
        try:
            self.cache.close()
        except Exception:
            pass
        super().closeEvent(ev)

    # -------------------------------------------------------- ① 扫描

    def _on_jobs_hint(self, *_a):
        """把「自动」解析成实际线程数显示出来 —— 免得用户不知道自己跑了几线程。"""
        import os
        v = int(self.cb_jobs.currentData() or 0)
        n = v if v > 0 else max(2, min(8, os.cpu_count() or 4))
        self.lb_jobs.setText(
            f"本次使用 {n} 个线程" if v > 0 else f"自动：本次使用 {n} 个线程")

    def _settings(self) -> ScanSettings:
        a, w, m = PRESETS.get(self.cb_preset.currentText(), PRESETS["标准"])
        return ScanSettings(root=Path(self.ed_root.text()), preset=self.cb_preset.currentText(),
                            anchors=a, window=w, max_pages=m, crop_mode=DEFAULT_CROP,
                            page_thr=self.sl_sim.value() / 100.0,
                            min_pages=self.sp_min.value(),
                            ratio_thr=self.sl_ratio.value() / 100.0,
                            threads=int(self.cb_jobs.currentData() or 0))

    def on_browse(self):
        d = QtWidgets.QFileDialog.getExistingDirectory(self, "选择漫画根目录",
                                                      self.ed_root.text() or str(Path.home()))
        if d:
            self.ed_root.setText(d)

    def on_scan(self):
        root = Path(self.ed_root.text().strip())
        if not self.ed_root.text().strip() or not root.exists():
            QtWidgets.QMessageBox.warning(self, "目录无效", "请先选择一个存在的漫画根目录。")
            return
        if self.scan_thread and self.scan_thread.isRunning():
            return
        self.root = root
        self._save_cfg()
        s = self._settings()
        self.tree.clear()
        self.groups = []
        self.txt_log.clear()
        self.logline("info", f"开始扫描：{root}")
        self.logline("info", f"采样预设「{s.preset}」：每本最多 {s.max_pages} 页；"
                             f"页级阈值 {s.page_thr:.2f}；最少匹配 {s.min_pages} 页")
        # 界面参数必须先在主线程快照成纯 Python 值再传给线程
        skip = QtWidgets.QMessageBox.question(
            self, "确认",
            f"将递归扫描：\n{root}\n\n"
            f"· 压缩包内的图片只在内存里读取，不会解压落盘\n"
            f"· 全程不联网、不上传任何数据\n"
            f"· 不会自动删除任何文件\n\n是否开始？",
            MB_YES | MB_NO,
            MB_YES)
        if skip != MB_YES:
            return

        self.pbar.setRange(0, 100)
        self.pbar.setValue(0)
        self.btn_scan.setEnabled(False)
        self.btn_pause.setEnabled(True)
        self.btn_stop.setEnabled(True)
        st = self.scan_thread = ScanThread(s, self.cache)
        st.sig_progress.connect(self.on_scan_progress)
        st.sig_log.connect(self.logline)
        st.sig_done.connect(self.on_scan_done)
        st.start()

    @Slot(int, int, str, object)
    def on_scan_progress(self, i: int, total: int, path: str, stats):
        self.pbar.setRange(0, max(1, total))
        self.pbar.setValue(i)
        self.pbar.setFormat(f"%p%  {i}/{total}")
        self.statusBar().showMessage(
            f"扫描 {i}/{total} · 缓存命中 {getattr(stats, 'n_cached', 0)} · "
            f"新算 {getattr(stats, 'n_scanned', 0)} · 异常 {getattr(stats, 'n_bad', 0)} · "
            f"{Path(path).name}")

    def on_pause(self):
        if not self.scan_thread or not self.scan_thread.worker:
            return
        w = self.scan_thread.worker
        if w.paused:
            w.resume()
            self.btn_pause.setText("暂停")
            self.logline("info", "已继续扫描")
        else:
            w.pause()
            self.btn_pause.setText("继续")
            self.logline("info", "已暂停（缓存已落盘，可以随时继续或关闭）")

    def on_stop(self):
        if self.scan_thread and self.scan_thread.worker:
            self.scan_thread.worker.stop()
            self.logline("info", "已请求停止…")

    def on_scan_done(self, stats):
        self.btn_scan.setEnabled(True)
        self.btn_pause.setEnabled(False)
        self.btn_pause.setText("暂停")
        self.btn_stop.setEnabled(False)
        self._refresh_cache_info()
        if stats is None:
            self.logline("error", "扫描异常结束")
            return
        self.logline("info", f"扫描结束：{stats.n_books} 本（缓存命中 {stats.n_cached} / "
                             f"新算 {stats.n_scanned} / 异常 {stats.n_bad}），"
                             f"{stats.n_pages} 页特征，耗时 {stats.elapsed:.1f} 秒")
        for p, e in (self.scan_thread.worker.errors[:20] if self.scan_thread.worker else []):
            self.logline("warn", f"跳过 {Path(p).name}：{e}")
        self.on_regroup(silent=True)

    # -------------------------------------------------------- ② 分组

    def on_regroup(self, silent: bool = False):
        if self.group_thread and self.group_thread.isRunning():
            return
        s = self._settings()
        gs = GroupSettings(page_thr=s.page_thr, min_pages=s.min_pages,
                           ratio_thr=s.ratio_thr, coarse_thr=s.coarse_thr,
                           threads=s.threads)
        if not silent:
            self.logline("info", f"重新分组：阈值 {gs.page_thr:.2f} / "
                                 f"最少 {gs.min_pages} 页 / 命中率 ≥ {gs.ratio_thr:.2f}")
        self.pbar.setRange(0, 0)
        self.statusBar().showMessage("正在比对分组…")
        gt = self.group_thread = GroupThread(self.cache, gs)
        gt.sig_progress.connect(lambda m: self.statusBar().showMessage(m))
        gt.sig_done.connect(self.on_group_done)
        gt.start()

    def on_group_done(self, groups, pairs, stats):
        self.pbar.setRange(0, 100)
        self.pbar.setValue(100)
        self.groups = groups or []
        self._render_groups()
        n_waste = sum(g.wasted for g in self.groups)
        self.lb_summary.setText(
            f"重复组 {len(self.groups)} 组，涉及 {sum(len(g.members) for g in self.groups)} 本，"
            f"可回收约 {human_size(n_waste)}"
            + (f"　｜ 候选书对 {stats.get('candidates')}，判重书对 {stats.get('pair_hits')}"
               if stats else ""))
        self.logline("info", f"分组完成：重复 {len(self.groups)} 组，可回收 {human_size(n_waste)}")
        self.statusBar().showMessage("分组完成", 5000)

    def _render_groups(self):
        self.tree.blockSignals(True)
        self.tree.clear()
        self.rows_by_group = {}
        for g in self.groups:
            top = QtWidgets.QTreeWidgetItem(self.tree)
            top.setText(0, f"组 {g.gid}　{len(g.members)} 本　可回收 {human_size(g.wasted)}")
            top.setText(3, f"{g.score:.3f}")
            top.setText(4, str(g.matched))
            top.setFirstColumnSpanned(False)
            top.setFlags(q("ItemFlag.ItemIsEnabled"))
            f = top.font(0)
            f.setBold(True)
            top.setFont(0, f)
            top.setBackground(0, QtGui.QBrush(QtGui.QColor("#eef3fb")))
            rows = []
            for m in g.members:
                it = QtWidgets.QTreeWidgetItem(top)
                it.setText(0, ("★ " if m.keep else "") + Path(m.path).name
                           + ("　（建议保留）" if m.keep else ""))
                it.setText(1, human_size(m.size))
                it.setText(2, str(m.pages))
                it.setText(3, f"{m.score:.3f}")
                it.setText(4, str(m.matched))
                it.setText(5, m.path)
                it.setToolTip(5, m.path)
                it.setToolTip(0, f"匹配 {m.matched} 页 / 命中率 {m.ratio:.0%} / "
                                 f"抽样 {m.sampled} 页\n{m.path}")
                it.setFlags(q("ItemFlag.ItemIsEnabled") | q("ItemFlag.ItemIsSelectable")
                            | q("ItemFlag.ItemIsUserCheckable"))
                it.setCheckState(0, q("CheckState.Unchecked"))
                ic = self._thumb_icon(m.thumb)
                if ic is not None:
                    it.setIcon(0, ic)
                it.setData(0, q("ItemDataRole.UserRole"), m)
                rows.append(it)
            top.setExpanded(True)
            self.rows_by_group[g.gid] = rows
        self.tree.blockSignals(False)
        self._apply_filter()

    def _thumb_icon(self, name: str):
        if not name:
            return None
        p = self.thumb_dir / name
        if not p.exists():
            return None
        pm = QtGui.QPixmap(str(p))
        if pm.isNull():
            return None
        return QtGui.QIcon(pm)

    # -------------------------------------------------------- 选择 / 详情

    def on_select(self):
        items = self.tree.selectedItems()
        if not items:
            self.lb_title.setText("（未选中）")
            self.lb_meta.setText("")
            self.lb_cover.setPixmap(QtGui.QPixmap())
            self.lb_cover.setText("封面预览")
            self.tb_pairs.setRowCount(0)
            return
        it = items[0]
        m: Member | None = it.data(0, q("ItemDataRole.UserRole"))
        if m is None and it.childCount():
            it = it.child(0)
            m = it.data(0, q("ItemDataRole.UserRole"))
        if m is None:
            return
        self.lb_title.setText(Path(m.path).name + ("　【建议保留】" if m.keep else ""))
        self.lb_meta.setText(
            f"载体：{'图片文件夹' if m.kind == 'folder' else '压缩包'}（{m.fmt}）\n"
            f"文件大小：{human_size(m.size)}\n"
            f"图片总页数：{m.pages}　　抽样比对：{m.sampled} 页\n"
            f"组内相似度：{m.score:.3f}　　匹配页数：{m.matched}　"
            f"命中率：{m.ratio:.0%}\n"
            f"路径：{m.path}")
        pm = self._preview_pixmap(m.thumb)
        if pm is not None:
            self.lb_cover.setPixmap(pm)
        else:
            self.lb_cover.setPixmap(QtGui.QPixmap())
            self.lb_cover.setText("（没有封面缩略图 —— 用「查看封面大图」直接从原文件读）")
        self.tb_pairs.setRowCount(min(len(m.pairs), 200))
        for r, (ia, ib, sc) in enumerate(m.pairs[:200]):
            self.tb_pairs.setItem(r, 0, QtWidgets.QTableWidgetItem(str(ia)))
            self.tb_pairs.setItem(r, 1, QtWidgets.QTableWidgetItem(str(ib)))
            self.tb_pairs.setItem(r, 2, QtWidgets.QTableWidgetItem(f"{sc:.3f}"))

    def _preview_pixmap(self, name: str):
        if not name:
            return None
        p = self.thumb_dir / name
        if not p.exists():
            return None
        pm = QtGui.QPixmap(str(p))
        if pm.isNull():
            return None
        return pm.scaledToHeight(PREVIEW_H, q("TransformationMode.SmoothTransformation"))

    def _items_checked(self) -> list:
        out = []
        for gid, rows in self.rows_by_group.items():
            for it in rows:
                if it.checkState(0) == q("CheckState.Checked"):
                    m = it.data(0, q("ItemDataRole.UserRole"))
                    if m is not None:
                        out.append(m)
        return out

    def _on_item_changed(self, item, col):
        if col != 0:
            return
        m = item.data(0, q("ItemDataRole.UserRole"))
        if m is None:
            return
        n = len(self._items_checked())
        self.lb_summary.setText(f"已勾选 {n} 项，合计 {human_size(sum(x.size for x in self._items_checked()))}"
                                f"　（点「删除选中文件」才会处理，且会二次确认）")

    def _apply_filter(self):
        kw = self.ed_filter.text().strip().lower()
        for i in range(self.tree.topLevelItemCount()):
            top = self.tree.topLevelItem(i)
            vis = False
            for j in range(top.childCount()):
                ch = top.child(j)
                hit = (not kw) or (kw in ch.text(0).lower()) or (kw in ch.text(5).lower())
                ch.setHidden(not hit)
                vis = vis or hit
            top.setHidden(not vis)

    # -------------------------------------------------------- 详情操作

    def _cur_member(self) -> Member | None:
        items = self.tree.selectedItems()
        if not items:
            return None
        it = items[0]
        m = it.data(0, q("ItemDataRole.UserRole"))
        if m is None and it.childCount():
            m = it.child(0).data(0, q("ItemDataRole.UserRole"))
        return m

    def on_open_folder(self):
        m = self._cur_member()
        if not m:
            return
        p = Path(m.path)
        try:
            if os.name == "nt":
                if p.is_dir():
                    os.startfile(str(p))            # noqa: S606
                else:
                    subprocess.Popen(["explorer", "/select,", str(p)])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(p)])
            else:
                subprocess.Popen(["xdg-open", str(p.parent)])
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "打开失败", str(e))

    def on_open_cover(self):
        m = self._cur_member()
        if not m:
            return
        try:
            from . import archives as A
            from . import core as C
            if m.kind == "folder":
                files = sorted([x for x in Path(m.path).iterdir()
                                if x.is_file() and x.suffix.lower() in A.IMG_EXT],
                               key=lambda x: A._natural_key(x.name))
                data = files[0].read_bytes() if files else b""
            else:
                bk = A.open_book(Path(m.path))
                try:
                    names = [n for n, _s in bk.list_images()]
                    data = bk.read(names[0]) if names else b""
                finally:
                    bk.close()
            if not data:
                raise RuntimeError("读不到封面")
            g = C.decode_gray(data, work_max=1400)
            if g is None:
                raise RuntimeError("封面解码失败")
            import cv2
            from PIL import Image
            import io as _io
            ok, buf = cv2.imencode(".png", g)
            im = Image.open(_io.BytesIO(buf.tobytes()))
            self._show_image_window(im, Path(m.path).name)
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "查看封面失败", f"{type(e).__name__}: {e}")

    def _show_image_window(self, pil_im, title):
        """弹一个独立的图片窗口（不阻塞主界面）。"""
        try:
            pil_im.thumbnail((900, 1300))
            buf = pil_im.convert("RGB").tobytes("raw", "RGB")
            qimg = QtGui.QImage(buf, pil_im.width, pil_im.height,
                                pil_im.width * 3, IMG_RGB888)
            dlg = QtWidgets.QDialog(self)
            dlg.setWindowTitle(f"封面预览 —— {title}")
            lay = QtWidgets.QVBoxLayout(dlg)
            lb = QtWidgets.QLabel()
            lb.setPixmap(QtGui.QPixmap.fromImage(qimg))
            lay.addWidget(lb)
            dlg.resize(pil_im.width + 30, pil_im.height + 30)
            dlg.show()
        except Exception as e:
            QtWidgets.QMessageBox.warning(self, "预览失败", str(e))

    def on_set_keep(self):
        m = self._cur_member()
        if not m:
            return
        for g in self.groups:
            if any(x.book_id == m.book_id for x in g.members):
                for x in g.members:
                    x.keep = (x.book_id == m.book_id)
                break
        self._render_groups()
        self.logline("info", f"已把「{Path(m.path).name}」设为该组建议保留项")

    # -------------------------------------------------------- ④ 导出 / 清缓存 / 删除

    def on_export(self):
        if not self.groups:
            QtWidgets.QMessageBox.information(self, "没有结果", "先扫描并分组，再导出。")
            return
        default = str((self.root or Path.home()) / "漫画重复清单.csv")
        fn, _ = QtWidgets.QFileDialog.getSaveFileName(self, "导出重复清单", default,
                                                     "CSV 文件 (*.csv)")
        if not fn:
            return
        try:
            n = export_csv(self.groups, Path(fn), root=self.root)
            self.logline("info", f"已导出 {n} 行到 {fn}")
            QtWidgets.QMessageBox.information(self, "导出完成", f"已写入 {n} 行：\n{fn}")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "导出失败", str(e))

    def on_clear(self):
        st = self.cache.stats()
        r = QtWidgets.QMessageBox.question(
            self, "清空缓存数据库",
            f"当前缓存：{st['books']} 本 / {st['sampled']} 页特征。\n\n"
            f"清空后下次扫描需要全部重算（3000 本可能要几小时）。\n"
            f"不会删除任何漫画文件。是否继续？",
            MB_YES | MB_NO,
            MB_NO)
        if r != MB_YES:
            return
        try:
            self.cache.clear()
            self.groups = []
            self._render_groups()
            self._refresh_cache_info()
            self.logline("info", "缓存数据库已清空")
        except Exception as e:
            QtWidgets.QMessageBox.critical(self, "清空失败", str(e))

    def on_delete(self):
        picked = self._items_checked()
        if not picked:
            QtWidgets.QMessageBox.information(
                self, "没有勾选", "请先在左侧列表里勾选要删除的单行本（复选框）。\n"
                                  "程序不会自动删除任何东西。")
            return
        total = sum(m.size for m in picked)
        lst = "\n".join(f"  · {m.path}" for m in picked[:25])
        more = f"\n  …另有 {len(picked) - 25} 项" if len(picked) > 25 else ""
        r = QtWidgets.QMessageBox.warning(
            self, "⚠ 二次确认：删除选中文件",
            f"即将把下列 {len(picked)} 项移入回收站（合计 {human_size(total)}）：\n\n"
            f"{lst}{more}\n\n"
            f"· 只会「移入系统回收站」，不会永久删除；\n"
            f"· 回收站不可用时会**移动到**同目录下的「_漫画查重回收站」文件夹；\n"
            f"· 每一项移动后都会校验，出错立即停止。\n\n"
            f"确定继续吗？",
            MB_YES | MB_NO,
            MB_NO)
        if r != MB_YES:
            self.logline("info", "已取消删除")
            return
        rd = (self.root / "_漫画查重回收站") if self.root else None
        self.btn_del.setEnabled(False)
        self.del_thread = DeleteThread([m.path for m in picked], rd)
        self.del_thread.sig_log.connect(self.logline)
        self.del_thread.sig_done.connect(self.on_delete_done)
        self.del_thread.start()

    def on_delete_done(self, res: dict):
        self.btn_del.setEnabled(True)
        ok, bad = len(res.get("moved", [])), len(res.get("failed", []))
        self.logline("info", f"删除完成：成功 {ok} 项，失败 {bad} 项")
        try:
            lp = data_dir() / "删除记录.csv"
            rows = list(res.get("log", []))
            old = []
            import csv as _csv
            if lp.exists():
                with open(lp, "r", newline="", encoding="utf-8-sig") as f:
                    old = list(_csv.reader(f))[1:]
            export_log(old + rows, lp)
            self.logline("info", f"删除记录已写入 {lp}")
        except Exception:
            pass
        QtWidgets.QMessageBox.information(
            self, "删除结果",
            f"成功移出 {ok} 项，失败 {bad} 项。\n\n"
            f"清单里已删掉的项仍会列在结果中，点「重新分组」即可刷新。")
        self.on_regroup(silent=True)


# ================================================================== 入口


def run_gui(argv=None) -> int:
    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(argv or sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(__version__)
    try:
        app.setStyle("Fusion")
    except Exception:
        pass
    # 精简系统 / 无桌面会话的环境里 Qt 可能一个字族都拿不到，那时中文会全变方块。
    # 这一步只在「系统没有中文字体」时才显式注册字体文件，正常桌面下不做任何事。
    try:
        from .qtcompat import _FONT_FAMILY, load_cjk_fonts

        if load_cjk_fonts() and _FONT_FAMILY:
            f = app.font()
            f.setFamily(_FONT_FAMILY)
            app.setFont(f)
    except Exception:
        pass
    cache = FeatureCache()
    w = MainWindow(cache)
    w.show()
    return exec_app(app)
