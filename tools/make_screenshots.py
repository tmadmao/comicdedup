"""生成 README 用的界面截图 —— 用**真实界面**离屏渲染，不是手画的示意图。

思路：
  1. 强制 ``QT_QPA_PLATFORM=offscreen``，Qt 不会创建任何可见窗口（本机纪律）；
  2. 显式注册系统里的微软雅黑字体文件 —— 离屏环境的 QFontDatabase 字体数是 0，
     不注册的话中文全是空心方块；
  3. 载入真实扫描结果（testdata.sqlite）+ 真实封面缩略图，跑完整分组逻辑，
     所以截图里显示的分组、相似度、页数都是程序真算出来的；
  4. ``widget.grab()`` 抓图，再用 PIL 加一圈窗口边框与标题栏，让它在 README 里像张真截图。

用法::

    python tools/make_screenshots.py [testdata目录] [sqlite路径]
"""

from __future__ import annotations

import os
import sys
import time
import traceback
from pathlib import Path

os.environ["QT_QPA_PLATFORM"] = "offscreen"          # 必须先于 Qt 导入
os.environ.setdefault("QT_SCALE_FACTOR", "2")        # 2 倍渲染，README 里放大也清晰

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from comicdedup.cache import FeatureCache                              # noqa: E402
from comicdedup.core import human_size                                 # noqa: E402
from comicdedup.engine import GroupSettings, group_books               # noqa: E402
from comicdedup.qtcompat import QtGui, QtWidgets, load_cjk_fonts, q    # noqa: E402

DOCS = ROOT / "docs"


# ================================================================== 窗口装饰

def decorate(png: Path, title: str) -> Path:
    """给抓下来的客户区加一圈窗口边框 + 标题栏，让 README 里看着像真窗口。"""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return png

    ss = 2
    im = Image.open(png).convert("RGB")
    pad_top, pad = 34 * ss, 10 * ss
    W, H = im.width + pad * 2, im.height + pad_top + pad
    canvas = Image.new("RGB", (W, H), (238, 240, 244))
    d = ImageDraw.Draw(canvas)

    # 标题栏
    d.rectangle([0, 0, W, pad_top], fill=(246, 247, 250))
    d.line([0, pad_top, W, pad_top], fill=(214, 218, 226), width=ss)
    fp = r"C:\Windows\Fonts\msyh.ttc"
    try:
        f = ImageFont.truetype(fp, 13 * ss)
    except Exception:
        f = ImageFont.load_default()
    d.text((12 * ss, 9 * ss), title, font=f, fill=(58, 63, 76))

    # 右上角三个假按钮（就是窗体的最小化/最大化/关闭，纯装饰）
    for i, col in enumerate(((196, 200, 208), (196, 200, 208), (206, 116, 110))):
        cx = W - (46 + i * 26) * ss
        d.ellipse([cx - 5 * ss, 17 * ss - 5 * ss, cx + 5 * ss, 17 * ss + 5 * ss], fill=col)

    canvas.paste(im, (pad, pad_top))
    canvas.save(png)
    return png


def shrink(png: Path) -> Path:
    """调色板量化 + 优化压缩。

    界面截图是大片纯色 + 灰阶抗锯齿文字，用自适应 256 色调色板几乎看不出差别
    （实测肉眼无差异），体积却能砍掉一半左右 —— README 里加载快一些。
    """
    try:
        from PIL import Image
    except Exception:
        return png
    try:
        im = Image.open(png).convert("RGB")
        q = im.convert("P", palette=Image.ADAPTIVE, colors=256)
        tmp = png.with_name(png.stem + ".opt.png")
        q.save(tmp, optimize=True)
        if tmp.stat().st_size < png.stat().st_size:
            tmp.replace(png)
        else:
            tmp.unlink(missing_ok=True)
    except Exception:
        pass
    return png


# ================================================================== 主流程

def main() -> int:
    lib = Path(sys.argv[1] if len(sys.argv) > 1 else ROOT / "testdata")
    db = Path(sys.argv[2] if len(sys.argv) > 2 else ROOT / "testdata.sqlite")
    if not db.exists():
        print(f"找不到特征库 {db} —— 先跑一次："
              f"python comic_dedup.py --scan \"{lib}\" --db \"{db}\"")
        return 1

    app = QtWidgets.QApplication.instance() or QtWidgets.QApplication(sys.argv)
    app.setStyle("Fusion")
    fam = load_cjk_fonts(force=True)          # 离屏环境必须强制注册
    if fam:
        f = app.font()
        f.setFamily(fam)
        app.setFont(f)
    print(f"中文字族：{fam or '(没找到，中文会变方块)'}")

    cache = FeatureCache(db)
    from comicdedup.gui import MainWindow

    w = MainWindow(cache)
    w.root = lib
    w.ed_root.setText(str(lib))
    w.resize(1440, 920)

    # ---- 用真实数据跑一次分组（同步调用，不进线程，脚本里更好控制）
    t0 = time.time()
    groups, _pairs, stats = group_books(cache, GroupSettings())
    w.groups = groups or []
    w._render_groups()
    n_waste = sum(g.wasted for g in w.groups)
    w.lb_summary.setText(
        f"重复组 {len(w.groups)} 组，涉及 {sum(len(g.members) for g in w.groups)} 本，"
        f"可回收约 {human_size(n_waste)}"
        + (f"　｜ 候选书对 {stats.get('candidates')}，判重书对 {stats.get('pair_hits')}"
           if stats else ""))
    print(f"分组完成：{len(w.groups)} 组（{time.time() - t0:.1f} 秒）")

    # 日志面板填上「像刚跑完一次」的真实记录
    w.logline("info", f"扫描根目录：{lib}")
    w.logline("info", f"识别到单行本 {stats.get('books', 0)} 本，"
                      f"共 {stats.get('pages', 0)} 页特征")
    for p, e in (("彻底损坏.zip", "压缩包损坏（BadZipFile）—— 已跳过"),):
        w.logline("warn", f"跳过 {p}：{e}")
    w.logline("info", f"候选书对 {stats.get('candidates')}，判重书对 {stats.get('pair_hits')}")
    w.logline("info", f"分组完成：重复 {len(w.groups)} 组，"
                      f"涉及 {sum(len(g.members) for g in w.groups)} 本，"
                      f"可回收 {human_size(n_waste)}")
    w.pbar.setRange(0, 100)
    w.pbar.setValue(100)
    w.statusBar().showMessage(
        "分组完成 · 全部处理都在本地完成，不联网、不上传")

    # ---- 选中「扫描版 vs 官方 DL 版」那一组（最能说明本项目的价值）
    # 优先找「组内既有官方DL又有自制扫描」的组 —— 这是需求里最难的一类重复。
    target_group = 0
    for i, g in enumerate(w.groups):
        names = " ".join(m.path for m in g.members)
        if ("DL" in names or "dl" in names) and ("扫描" in names or "自制" in names):
            target_group = i
            break
    else:
        for i, g in enumerate(w.groups):
            if any("扫描" in m.path or "DL" in m.path for m in g.members):
                target_group = i
                break
    top = w.tree.topLevelItem(target_group)
    if top is not None and top.childCount():
        w.tree.expandAll()
        w.tree.setCurrentItem(top.child(0))
        w.on_select()
        print(f"选中：{top.text(0)} → {top.child(0).text(0)}")

    # 勾掉「非保留项」，让截图体现「人工勾选待删除」的使用姿态
    for i in range(top.childCount()):
        it = top.child(i)
        m = it.data(0, q("ItemDataRole.UserRole"))
        if m is not None and not m.keep and i > 0:
            it.setCheckState(0, q("CheckState.Checked"))

    app.processEvents()

    DOCS.mkdir(parents=True, exist_ok=True)

    # ---- 1) 整窗截图
    m = ROOT / "docs" / "ui-main.png"
    pm = w.grab()
    if not pm.save(str(m)):
        print("截图失败")
        return 1
    decorate(m, w.windowTitle())
    shrink(m)

    # ---- 2) 只抓分组树区域（README 里当特写用）
    m2 = ROOT / "docs" / "ui-groups.png"
    pm2 = w.tree.grab()
    if pm2.save(str(m2)):
        decorate(m2, "② 重复组（勾选要删除的项）—— 封面预览 / 名称 / 体积 / 页数 / 相似度 / 完整路径")
        shrink(m2)

    print(f"已生成 {m.name}（{m.stat().st_size / 1024:.0f} KB）、"
          f"{m2.name}（{m2.stat().st_size / 1024:.0f} KB）")

    try:
        w.close()
    except Exception:
        pass
    cache.close()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(2)
