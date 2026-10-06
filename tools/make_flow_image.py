"""生成 README 用的「核心算法流程图」—— 直接画一张 PNG，不依赖浏览器。

为什么用 PIL 手画而不用现成的画图库：
  * 想画成什么样就画成什么样，中文字体直接用系统字体文件（不走 Qt 字体库）；
  * 3 倍超采样再缩回，边缘干净，没有锯齿；
  * 零额外依赖（Pillow 本来就有，是读图必需）。

用法::

    python tools/make_flow_image.py        # 输出 docs/algorithm-flow.png
"""

from __future__ import annotations

import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "algorithm-flow.png"

SS = 3                                    # 超采样倍数
FONT_R = r"C:\Windows\Fonts\msyh.ttc"     # 微软雅黑
FONT_B = r"C:\Windows\Fonts\msyhbd.ttc"   # 微软雅黑 粗体

# ---- 配色（浅色主题，放进 GitHub README 里不刺眼）
BG = (255, 255, 255)
CARD = (247, 249, 252)
CARD_B = (223, 228, 236)
INK = (31, 36, 48)
INK2 = (88, 96, 110)
MUTED = (120, 130, 145)
PHASES = [(47, 111, 237), (122, 90, 248), (15, 157, 118), (232, 121, 43)]
WARN_BG = (255, 248, 235)
WARN_B = (240, 194, 104)
WARN_INK = (146, 84, 10)
GOOD = (15, 157, 118)
BAD = (214, 69, 69)


def F(size: int, bold: bool = False):
    return ImageFont.truetype(FONT_B if bold else FONT_R, size * SS)


# ------------------------------------------------------------------ 绘图小工具

def rrect(d, box, r, fill=None, outline=None, w=1):
    d.rounded_rectangle([c * SS for c in box], radius=r * SS, fill=fill,
                        outline=outline, width=max(1, int(w * SS)))


def text(d, xy, s, font, fill=INK, anchor="la"):
    d.text((xy[0] * SS, xy[1] * SS), s, font=font, fill=fill, anchor=anchor)


def tw(d, s, font) -> float:
    """文本宽度（逻辑像素）。"""
    return d.textlength(s, font=font) / SS


def arrow_right(d, x0, y, x1, color=(160, 170, 185), w=2):
    """一条带箭头的水平线（指向右）。"""
    d.line([(x0 * SS, y * SS), ((x1 - 5) * SS, y * SS)], fill=color, width=int(w * SS))
    d.polygon([((x1 - 7) * SS, (y - 5) * SS), ((x1 - 7) * SS, (y + 5) * SS),
               (x1 * SS, y * SS)], fill=color)


def tri_warn(d, x, y, size, fill):
    """画一个警告三角。

    ⚠ 用画的而不是打字符：微软雅黑里没有 U+26A0 的字形，直接打会显示成空心方块。
    """
    h = size
    d.polygon([(x * SS, (y + h) * SS), ((x + h) * SS, (y + h) * SS),
               ((x + h / 2) * SS, y * SS)], fill=fill)
    d.rectangle([(x + h / 2 - 1.4) * SS, (y + h * 0.34) * SS,
                 (x + h / 2 + 1.4) * SS, (y + h * 0.78) * SS], fill=(255, 255, 255))
    d.rectangle([(x + h / 2 - 1.4) * SS, (y + h * 0.84) * SS,
                 (x + h / 2 + 1.4) * SS, (y + h * 0.92) * SS], fill=(255, 255, 255))


def diamond(d, x, y, size, fill):
    """小菱形符号（替代字体里的 ▸，同样是出于字形缺失的考虑）。"""
    d.polygon([(x * SS, (y + size / 2) * SS), ((x + size / 2) * SS, y * SS),
               ((x + size) * SS, (y + size / 2) * SS),
               ((x + size / 2) * SS, (y + size) * SS)], fill=fill)


# ------------------------------------------------------------------ 主体

def main() -> int:
    W, H = 1500, 492
    img = Image.new("RGB", (W * SS, H * SS), BG)
    d = ImageDraw.Draw(img)

    f_title = F(25, True)
    f_sub = F(12.5)
    f_ph = F(14, True)
    f_item = F(12.5)
    f_item_b = F(12.5, True)
    f_note = F(11)
    f_tbl_h = F(11.5, True)
    f_tbl = F(11.5)

    # ============ 标题 ============
    text(d, (26, 18), "漫画查重 · 核心算法流程", f_title, INK)
    sub = ("全程本地完成：不联网 · 不上传图片或特征 · 不做 OCR · 压缩包不落地解压"
           "　｜　一个压缩包 / 一个图片文件夹 = 一本单行本")
    text(d, (26, 52), sub, f_sub, MUTED)

    # ============ 四个阶段 ============
    phases = [
        ("① 采集", [
            ("递归扫描根目录", 0),
            ("识别 zip / rar / 7z / cbz", 0),
            ("识别 jpg/png/webp 文件夹", 0),
            ("跳过损坏压缩包与坏图，写日志不崩", 1),
        ]),
        ("② 特征", [
            ("内存读取图片，不落地解压", 0),
            ("灰度 → 只去盖板黑边 → 去斜", 0),
            ("光照校正 → 对比度归一 → 64×64", 0),
            ("pHash + 版式哈希 + 16×16 粗筛图", 0),
            ("存 SQLite：第二次扫描秒过", 1),
        ]),
        ("③ 比对", [
            ("候选筛选：16×16 批量余弦 ≥0.55", 0),
            ("精比：matchTemplate 平移搜索 ±12.5%", 1),
            ("融合：0.75×精比 + 0.25×pHash", 0),
            ("判重：命中页≥3 且 命中率≥0.25 且 页序一致", 1),
            ("并查集分组（组代表互验防连环并组）", 0),
        ]),
        ("④ 交付", [
            ("分组列表 + 封面缩略图 + 相似度", 0),
            ("每项带复选框，默认一个都不勾", 1),
            ("导出重复清单 CSV（路径/页数/相似度）", 0),
            ("删除：移入回收站，二次弹窗确认", 1),
        ]),
    ]

    x0, gap = 26, 16
    card_w = (W - x0 * 2 - gap * 3) / 4
    top, header_h, row_h, pad = 86, 40, 27, 12
    max_rows = max(len(p[1]) for p in phases)
    card_h = header_h + pad + max_rows * row_h + 10

    for i, (name, items) in enumerate(phases):
        cx = x0 + i * (card_w + gap)
        color = PHASES[i]
        rrect(d, (cx, top, cx + card_w, top + card_h), 10, fill=CARD, outline=CARD_B, w=1)
        # 阶段标题条
        d.rounded_rectangle([cx * SS, top * SS, (cx + card_w) * SS, (top + header_h) * SS],
                            radius=10 * SS, fill=color)
        d.rectangle([cx * SS, (top + header_h - 12) * SS, (cx + card_w) * SS,
                     (top + header_h) * SS], fill=color)
        text(d, (cx + 14, top + 11), name, f_ph, (255, 255, 255))

        for j, (s, strong) in enumerate(items):
            iy = top + header_h + pad + j * row_h
            if strong:
                diamond(d, cx + 16, iy + 4, 9, color)
            else:
                d.ellipse([(cx + 17) * SS, (iy + 6) * SS, (cx + 22) * SS, (iy + 11) * SS],
                          fill=PHASES[i])
            fnt = f_item_b if strong else f_item
            text(d, (cx + 30, iy), s, fnt, INK if strong else INK2)

        if i < 3:
            arrow_right(d, cx + card_w + 3, top + header_h / 2 - 2, cx + card_w + gap - 3,
                        color=(178, 186, 198), w=2)

    # 阶段之间的衔接说明
    y_note = top + card_h + 10
    text(d, (x0 + 2, y_note),
         "每一页都独立提特征 → 同一本书的多页构成「页特征集合」→ 两本书之间只要达到阈值数量的内页匹配上，就判为重复",
         f_note, MUTED)

    # ============ 关键结论条 ============
    cy = y_note + 26
    ch = H - cy - 22
    rrect(d, (x0, cy, W - x0, cy + ch), 10, fill=WARN_BG, outline=WARN_B, w=1.5)

    text(d, (x0 + 18, cy + 13), "", F(15, True), WARN_INK)
    tri_warn(d, x0 + 17, cy + 14, 15, WARN_INK)
    text(d, (x0 + 42, cy + 14), "最反直觉、也是踩坑最多的一个结论：裁剪越「准」，查重结果越差",
         F(14.5, True), WARN_INK)

    # 对比表
    tx, ty = x0 + 42, cy + 46
    cols = [230, 108, 108, 210]
    heads = ["裁剪方式", "同页中位相似度", "异页最高分", "多页投票（K=3）"]
    rows = [
        ("只去扫描仪盖板黑边　← 最终采用", "0.814", "0.497", "同书 3/3 命中、跨书 0/6 误报", True),
        ("盖板 + 墨迹尺度归一（分位数）", "0.482", "0.787", "全部失效", False),
        ("盖板 + 高频边缘逐边硬裁", "0.358", "0.714", "全部失效", False),
    ]
    for k, h in enumerate(heads):
        text(d, (tx + sum(cols[:k]) + 4, ty), h, f_tbl_h, MUTED)
    d.line([(tx * SS, (ty + 18) * SS), ((tx + sum(cols)) * SS, (ty + 18) * SS)],
           fill=WARN_B, width=max(1, SS))
    for r, (name, mid, top_, vote, ok) in enumerate(rows):
        ry = ty + 24 + r * 21
        text(d, (tx + 4, ry), name, f_tbl, WARN_INK if ok else INK2)
        text(d, (tx + cols[0] + 22, ry), mid, f_tbl, GOOD if ok else BAD)
        text(d, (tx + cols[0] + cols[1] + 22, ry), top_, f_tbl, INK2)
        text(d, (tx + cols[0] + cols[1] + cols[2] + 4, ry), vote,
             f_tbl, GOOD if ok else BAD)

    # 原因 / 解法
    rx = tx + sum(cols) + 30
    rw = W - x0 - 20 - rx
    d.line([(rx - 14) * SS, (ty - 2) * SS, (rx - 14) * SS, (ty + 88) * SS],
           fill=WARN_B, width=max(1, SS))
    text(d, (rx, ty - 2), "根因", F(12, True), WARN_INK)
    text(d, (rx, ty + 18),
         "自适应阈值型裁剪在「扫描版（噪点多）」和「官方 DL 版（干净）」上裁掉的量必然不同，",
         f_note, INK2)
    text(d, (rx, ty + 35),
         "反而引入了相对尺度差 —— 而平移搜索修不了尺度。这条路试了 5 版全部失败。",
         f_note, INK2)
    text(d, (rx, ty + 58), "解法", F(12, True), WARN_INK)
    text(d, (rx, ty + 78),
         "不再追求完美裁剪：只做尺度归一，让度量自己把残余错位搜出来（平移不变度量）。",
         f_note, INK2)

    img = img.resize((W, H), Image.LANCZOS)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # 自适应 256 色调色板：这张图是纯色块 + 文字抗锯齿，量化后肉眼无差异，体积能小一半
    img.convert("P", palette=Image.ADAPTIVE, colors=256).save(OUT, optimize=True)
    print(f"已生成 {OUT}（{OUT.stat().st_size / 1024:.0f} KB，{W}×{H}）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
