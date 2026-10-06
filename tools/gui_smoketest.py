"""图形界面回归测试 —— **off-screen 无窗口**运行。

⚠ 本机纪律：绝不在桌面创建真实窗口（视频查重项目跑 GUI 回归把用户桌面卡死过）。
这里强制 ``QT_QPA_PLATFORM=offscreen``，Qt 不会创建任何可见窗口，
但控件依然会完整布局、可以截图（``widget.grab()``），所以回归是真实有效的。

用法：
    python tools/gui_smoketest.py
"""

from __future__ import annotations

import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"          # 必须先于 Qt 导入
os.environ.setdefault("QT_LOGGING_RULES", "*=false")

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from comicdedup import core                                    # noqa: E402
from comicdedup.cache import FeatureCache                      # noqa: E402
from comicdedup.engine import Group, Member                    # noqa: E402
from comicdedup.qtcompat import QT_BINDING, enum, q            # noqa: E402
from comicdedup.qtcompat import QtCore, QtGui, QtWidgets       # noqa: E402

OK = 0
FAIL = 0
FAILED = []


def chk(cond, name, extra=""):
    global OK, FAIL
    if cond:
        OK += 1
        print(f"  [OK]   {name}" + (f"   {extra}" if extra else ""))
    else:
        FAIL += 1
        FAILED.append(name)
        print(f"  [FAIL] {name}   {extra}")
    return bool(cond)


def fake_groups(root: Path) -> list:
    """造两组假的重复结果（带真实文件与缩略图，才能验证渲染路径）。"""
    from PIL import Image
    import io as _io

    def thumb(seed):
        """造一张假缩略图，返回 JPEG 字节（缩略图存在库里，不落盘）。"""
        a = Image.new("L", (300, 430), 255)
        px = a.load()
        for y in range(0, 430, 7):
            for x in range(0, 300, 5):
                if (x * 3 + y * 7 + seed * 13) % 11 < 4:
                    for dy in range(6):
                        for dx in range(4):
                            if x + dx < 300 and y + dy < 430:
                                px[x + dx, y + dy] = 20
        buf = _io.BytesIO()
        a.save(buf, "JPEG", quality=80)
        return buf.getvalue()

    def mkfile(rel, size):
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * size)
        return p

    g1 = Group(gid=1, score=0.91, matched=6)
    g1.members = [
        Member(book_id=1, path=str(mkfile("火影忍者 第01卷 [官方DL].7z", 3200)),
               kind="archive", fmt="7z", size=3_279_320, pages=180, sampled=40,
               px=3_200_000, thumb_img=thumb(1), keep=False, score=0.91,
               matched=6, ratio=0.86, pairs=[(11, 13, 0.95), (29, 31, 0.93), (47, 49, 0.90)]),
        Member(book_id=2, path=str(mkfile("火影忍者 第01卷 [自制扫描].zip", 2600)),
               kind="archive", fmt="zip", size=2_716_434, pages=182, sampled=40,
               px=3_100_000, thumb_img=thumb(2), keep=True, score=0.88,
               matched=5, ratio=0.72, pairs=[(10, 13, 0.94), (28, 31, 0.91)]),
    ]
    g2 = Group(gid=2, score=1.0, matched=7)
    g2.members = [
        Member(book_id=3, path=str(mkfile("海贼王 第10卷/f1.jpg", 10)),
               kind="folder", fmt="folder", size=5_833_654, pages=8, sampled=7,
               px=2_050_000, thumb_img=thumb(3), keep=True, score=1.0, matched=7,
               ratio=1.0, pairs=[(1, 1, 1.0)]),
        Member(book_id=4, path=str(mkfile("海贼王 第10卷 (重打包).7z", 12)),
               kind="archive", fmt="7z", size=2_103_574, pages=8, sampled=7,
               px=2_050_000, thumb_img=b"", keep=False, score=1.0, matched=7, ratio=1.0, pairs=[]),
    ]
    g3 = Group(gid=3, score=0.7, matched=3)
    g3.members = [
        Member(book_id=5, path=str(mkfile("第三组 A.zip", 9)), kind="archive", fmt="zip",
               size=100, pages=9, sampled=9, px=1000, thumb_img=b"", keep=True, score=0.7,
               matched=3, ratio=0.33, pairs=[]),
        Member(book_id=6, path=str(mkfile("第三组 B.rar", 9)), kind="archive", fmt="rar",
               size=100, pages=9, sampled=9, px=1000, thumb_img=b"", keep=False, score=0.7,
               matched=3, ratio=0.33, pairs=[]),
    ]
    return [g1, g2, g3]


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="cdgui_"))
    os.environ["COMICDEDUP_DATA_DIR"] = str(tmp / "data")
    (tmp / "data").mkdir(parents=True, exist_ok=True)
    lib = tmp / "lib"
    lib.mkdir(parents=True, exist_ok=True)

    print(f"== 图形界面回归（off-screen 无窗口）· Qt 绑定：{QT_BINDING or '缺失'} ==")
    if not QT_BINDING:
        print("  [FAIL] 没有 Qt 绑定可用")
        return 1

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")

    # 离屏环境的 QFontDatabase 字体数是 0，中文会渲染成空心方块；显式注册字体文件。
    # 不影响判定结果（回归只查控件状态与文本），但截图存证可读了。
    from comicdedup.qtcompat import _FONT_FAMILY, load_cjk_fonts

    fam = load_cjk_fonts(force=True)
    if fam:
        f = app.font()
        f.setFamily(fam)
        app.setFont(f)

    cache = FeatureCache(tmp / "t.sqlite")
    from comicdedup.gui import MainWindow
    w = MainWindow(cache)
    chk(w is not None, "主窗口构建成功")

    # ---- 1. 渲染分组
    w.groups = fake_groups(lib)
    w.root = lib
    w._render_groups()
    chk(w.tree.topLevelItemCount() == 3, "渲染出 3 个重复组",
        f"实际 {w.tree.topLevelItemCount()}")
    chk(w.tree.columnCount() == 6, "6 列（名称/大小/页数/相似度/匹配页数/路径）")
    g1 = w.tree.topLevelItem(0)
    chk(g1.childCount() == 2, "第 1 组下有 2 项", f"实际 {g1.childCount()}")
    it0 = g1.child(0)
    chk(it0.data(0, q("ItemDataRole.UserRole")) is not None, "每项都挂了数据对象")
    chk(bool(it0.flags() & q("ItemFlag.ItemIsUserCheckable")), "每项带复选框")
    chk(it0.checkState(0) == q("CheckState.Unchecked"), "复选框默认不勾选（不会自动删）")
    chk(it0.text(1) != "", "有文件大小列")
    chk(it0.text(2).isdigit(), "有图片页数列", it0.text(2))
    chk(it0.text(3) != "", "有相似度列")
    chk("\\" in it0.text(5) or "/" in it0.text(5), "有完整路径列")
    chk(not it0.icon(0).isNull(), "没有缩略图时图标为空？", "有缩略图")
    # 建议保留标记
    marks = [g1.child(i).text(0) for i in range(g1.childCount())]
    chk(any("建议保留" in m for m in marks), "标出建议保留项", str(marks))

    # ---- 2. 筛选
    w.ed_filter.setText("海贼")
    w._apply_filter()
    vis = 0
    for i in range(w.tree.topLevelItemCount()):
        t = w.tree.topLevelItem(i)
        if not t.isHidden():
            vis += t.childCount()
    chk(vis == 2, "筛选「海贼」只剩 2 项", f"实际 {vis}")
    w.ed_filter.setText("")
    w._apply_filter()
    chk(all(not w.tree.topLevelItem(i).isHidden() for i in range(3)), "清空筛选后全部恢复")

    # ---- 3. 选中 → 详情
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    it0 = w.tree.topLevelItem(0).child(0)
    w.on_select()
    chk("火影" in w.lb_title.text(), "详情标题跟随选中", w.lb_title.text())
    chk("压缩包" in w.lb_meta.text() and "页" in w.lb_meta.text(), "详情显示载体与页数")
    chk(not w.lb_cover.pixmap().isNull(), "预览区显示出封面")
    chk(w.tb_pairs.rowCount() == len(
        it0.data(0, q("ItemDataRole.UserRole")).pairs), "匹配页表格行数正确",
        f"{w.tb_pairs.rowCount()} 行")

    # ---- 4. 勾选
    g1 = w.tree.topLevelItem(0)
    it0 = g1.child(0)
    it0.setCheckState(0, q("CheckState.Checked"))
    picked = w._items_checked()
    chk(len(picked) == 1, "勾选后能被正确统计", f"{len(picked)} 项")
    chk("已勾选 1 项" in w.lb_summary.text(), "底部提示勾选状态", w.lb_summary.text()[:40])
    g1.child(1).setCheckState(0, q("CheckState.Checked"))
    chk(len(w._items_checked()) == 2, "再勾一项共 2 项")
    it0.setCheckState(0, q("CheckState.Unchecked"))
    g1.child(1).setCheckState(0, q("CheckState.Unchecked"))
    chk(len(w._items_checked()) == 0, "取消勾选后归零")

    # ---- 5. 设为保留项
    # 注意：_render_groups 会重建整棵树，旧 item 句柄随之失效，必须重新取
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    w.on_set_keep()
    g1 = w.tree.topLevelItem(0)
    m0 = g1.child(0).data(0, q("ItemDataRole.UserRole"))
    m1 = g1.child(1).data(0, q("ItemDataRole.UserRole"))
    chk(m0.keep and not m1.keep, "设为保留项后组内互斥",
        f"{Path(m0.path).name}={m0.keep} / {Path(m1.path).name}={m1.keep}")

    # ---- 6. 导出 CSV（直接调函数，避开文件对话框）
    from comicdedup.engine import export_csv
    csvp = tmp / "dup.csv"
    n = export_csv(w.groups, csvp, root=lib)
    chk(csvp.exists() and n == 6, f"导出 CSV 成功（{n} 行）")
    head = csvp.read_text(encoding="utf-8-sig").splitlines()[0]
    chk("相似度" in head and "完整路径" in head and "建议保留" in head,
        "CSV 表头含路径/页数/相似度", head[:60])
    chk("★保留" in csvp.read_text(encoding="utf-8-sig"), "CSV 标出建议保留")

    # ---- 7. 滑块联动
    w.sl_sim.setValue(80)
    chk(w.lb_sim.text() == "0.80", "相似度滑块联动标签", w.lb_sim.text())
    chk("严格" in w.sl_sim.toolTip(), "滑块有宽松/严格提示", w.sl_sim.toolTip()[:24])
    w.sl_sim.setValue(62)

    # ---- 8. 参数快照进线程（不能在子线程读控件 —— 视频查重项目踩过的坑）
    w.ed_root.setText(str(lib))
    s = w._settings()
    chk(isinstance(s.page_thr, float) and isinstance(s.min_pages, int),
        "界面参数被快照成纯 Python 值再传给线程",
        f"thr={s.page_thr} K={s.min_pages}")

    # ---- 9. 删除的安全约束（只验逻辑，不真删）
    import inspect
    from comicdedup import engine as E
    src = inspect.getsource(E.delete_paths)
    chk("send2trash" in src and "shutil.move" in src, "删除走回收站/本地回收目录")
    chk("rmtree" not in src and "os.remove" not in src and "unlink(" not in src,
        "没有任何永久删除调用")
    chk("源不存在" in src or "gone" in src, "以「源是否还存在」为最终判据")
    # 真删一个临时文件（验证回收路径可用），并确认不是彻底删除
    victim = lib / "第三组 A.zip"
    r = E.delete_paths([str(victim)], recycle_dir=lib / "_rc", use_trash=False)
    moved = len(r.get("moved", []))
    chk(moved == 1 and not victim.exists(), "删除后源文件确实移走",
        f"moved={moved} exists={victim.exists()}")
    chk(any((lib / "_rc").glob("*")) if (lib / "_rc").exists() else False,
        "文件被移动到本地回收目录（不是彻底删除）")

    # ---- 10. 截图存证（off-screen 渲染，不是真窗口）
    # 只当作「抓图这条路走得通」的存证，写到临时目录；README 用的图由
    # tools/make_screenshots.py 生成（那边会用真实语料 + 窗口边框）。
    w.resize(1400, 900)
    w.tree.setCurrentItem(w.tree.topLevelItem(0).child(0))
    w.on_select()
    app.processEvents()
    shot = tmp / "shot-main.png"
    pm = w.grab()
    ok = pm.save(str(shot))
    chk(ok and shot.exists() and shot.stat().st_size > 20000,
        "界面截图已生成", f"{shot.stat().st_size if shot.exists() else 0} 字节")
    shot2 = tmp / "shot-groups.png"
    chk(pm.save(str(shot2)), "第二张截图已生成")

    try:
        w.close()
    except Exception:
        pass
    cache.close()

    print("")
    print(f"== 图形界面回归：通过 {OK} 项，失败 {FAIL} 项 ==")
    if FAILED:
        print("失败项：" + "、".join(FAILED))
    print(f"（临时目录：{tmp}）")
    return 1 if FAIL else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
