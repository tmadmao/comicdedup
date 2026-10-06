# -*- coding: utf-8 -*-
"""把 dist/ 里的 exe 发布为 GitHub Release，并设置仓库简介与话题。

说明：
  * Release 无法通过 git 推送完成，只能调 GitHub REST API；
  * 凭据从本机 Git 凭据管理器读取（``git credential fill``），
    **只在内存中使用，不写入磁盘、不打印到输出**；
  * 已存在同名 release / asset 时会先更新说明、删掉同名附件再上传，可重复执行；
  * 上传完会回读服务端计算的 sha256（``assets[].digest``）与本地比对，
    不用把上百 MB 的附件下载回来就能确认没传坏。

用法::

    python tools/gh_release.py --meta             # 只设置仓库简介与话题
    python tools/gh_release.py                    # 发布默认版本
    python tools/gh_release.py --version v1.0.1
    python tools/gh_release.py --no-upload        # 只建 release，不上传附件
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OWNER, REPO = "tmadmao", "comicdedup"
API = "https://api.github.com"
DEFAULT_VERSION = "v1.0.0"

REPO_DESC = ("Windows 本地漫画查重工具：扫描 zip/rar/7z/图片文件夹构成的单行本，"
             "识别重复组（含「自制扫描版 vs 官方 DL 版」）。纯离线运行，不联网不上传，"
             "不自动删除。")
REPO_TOPICS = ["comic", "manga", "deduplication", "duplicate-detection",
               "perceptual-hash", "phash", "opencv", "pyside6", "offline",
               "windows-desktop", "privacy", "cbz"]

CONTENT_TYPES = {
    ".exe": "application/vnd.microsoft.portable-executable",
    ".zip": "application/zip",
    ".txt": "text/plain; charset=utf-8",
    ".md": "text/markdown; charset=utf-8",
}


def get_token() -> str:
    """从 Git 凭据管理器取 GitHub 凭据（不打印、不落盘）。

    注意：凭据管理器首次调用可能要 5~10 秒（尤其刚连上代理时），超时给宽一点，
    否则会误判成「没有凭据」。
    """
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, cwd=str(ROOT), timeout=180,
            env={**os.environ, "GIT_TERMINAL_PROMPT": "0"},
        )
    except Exception as e:
        print(f"读取 Git 凭据失败：{e}")
        print("  提示：可先手动执行  git credential fill  确认凭据可用（应输出 password=...）")
        return ""
    for line in p.stdout.splitlines():
        if line.startswith("password="):
            return line[len("password="):].strip()
    return ""


def api(path_or_url: str, method: str = "GET", data: bytes | None = None,
        token: str = "", ctype: str = "application/json", timeout: int = 180):
    url = path_or_url if path_or_url.startswith("http") else API + path_or_url
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("User-Agent", "comicdedup-release")
    if data is not None:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
            return r.status, (json.loads(raw.decode("utf-8")) if raw else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "ignore")
        try:
            body = json.loads(body).get("message", body)
        except Exception:
            pass
        return e.code, {"message": body}
    except Exception as e:
        return 0, {"message": f"{type(e).__name__}: {e}"}


def build_body(version: str, assets: list) -> str:
    rows = "\n".join(
        f"| `{p.name}` | {p.stat().st_size / 1048576:.1f} MB | {note} |"
        for p, note in assets
    )
    sha_lines = "\n".join(
        f"{hashlib.sha256(p.read_bytes()).hexdigest()}  {p.name}" for p, _ in assets
    )
    return f"""## 漫画查重 ComicDedup {version}

Windows 本地漫画查重桌面程序。**纯本地离线运行**，专门解决「几千本单行本里
哪些是同一本书的重复版本」，尤其能识别最难的一类：**同一本漫画，一份是自制书本扫描版、
另一份是官方 DL 电子版**。

```
一个压缩包（zip / rar / 7z / cbz / cbr / cb7）  =  一本单行本
一个装着 jpg/png/webp 的图片文件夹               =  一本单行本
```

### 📦 下载

| 文件 | 大小 | 说明 |
|---|---|---|
{rows}

**普通用户下载上面的 zip**，解压 → 双击文件夹里的 `ComicDedupTool.exe` 即可，
不用装 Python、不用装任何依赖。压缩包 105 MB，解开后约 259 MB。

> **7z / rar 的解压需要本机装有 [7-Zip](https://www.7-zip.org/) 或 WinRAR**（zip 不需要）。
> 也可以把 `7z.exe` 直接放到 exe 同目录，程序会自动识别。

> 想要**单文件 exe**（只有一个文件、更好管理）可以自己打：
> `python -m PyInstaller --onefile --noconsole --noconfirm --name ComicDedupTool-onefile
> --collect-submodules comicdedup --hidden-import PySide6 comic_dedup.py`
> 代价是每次启动要把约 260MB 解压到临时目录（等 5~10 秒），且更容易被杀软误报。

### ⚠️ 关于杀软误报（务必先看这条）

这是一个**未做代码签名**的自制工具，打包后是全新文件、没有任何"云信誉"，
**被国产杀软的启发式引擎误报是常见现象**。本机实测过一次：

```
360 安全卫士 · 主动防御
  动作：进程创建
  路径：...\\dist\\ComicDedupTool.exe
  木马名称：HEUR/QVM202.0.8C7D.Malware.Gen
  处置：已清除（自动阻止，无提示）
```

怎么看这个判定名：`HEUR` = 启发式，`QVM` = 360 的机器学习引擎，
`.Malware.Gen` = **泛化特征**（"长得像"而不是"匹配到某个已知木马"）。
这三点加起来就是典型的**误报**，不是真的检出。

**为什么会被误报**：单文件 exe 的运行方式是"把内置的一大包依赖解压到临时目录再执行"，
这个行为与"释放载荷的木马"在行为特征上高度重合；再加上文件未签名、从没见过，
机器学习模型就容易给出可疑分。实测把打包方式从单文件换成**文件夹版**后，
同样的代码、同样的功能，就不再被拦（文件夹版没有自解压行为）。

**怎么办**，按推荐顺序：

1. **用文件夹版**（本页的 zip）：实测没被 360 拦过，而且启动更快。
2. 被拦了就在 360 里**加信任**：`360 → 木马查杀 → 信任区 → 添加目录`，把解压出来的文件夹加进去。
3. 360 隔离区里被删的文件可以**恢复**（它会标注"已清除/已隔离"，恢复后加信任即可）。

**想自己确认这个 exe 干不干净**，有三条不依赖厂商的路子：

* 源码就在本仓库里，`comic_dedup.py` + `comicdedup/` 一共 8 个模块，全中文注释，可以直接读；
  代码里**没有任何网络调用**（`--selftest` 有一项静态检查逐文件核验，跑一下就能看到）。
* 自己打包一遍比对：`打包exe.bat`，或者照 README 的说明跑 PyInstaller 命令。
* 核对本页下方的 SHA256，确认下载到的文件没被中间篡改。

### ✨ 核心能力

- **目录递归扫描**：识别压缩包与图片文件夹作为单行本；压缩包**在内存里读取图片，
  不落地解压**；损坏压缩包 / 坏图只记日志跳过，程序不崩溃。
- **跨版本识别**：不只比封面 —— 每本随机抽 5~40 张**非空白内页**提特征，
  靠「整本多页投票」判重，所以自制扫描版和官方 DL 版能归到同一组。
- **平移不变度量**（本项目最关键的一步）：不追求完美裁剪，而是让度量自己把
  两版之间的错位搜出来。详见下方标定数据。
- **特征缓存**：SQLite 缓存每本的页特征，第二次扫描是秒级。
- **人工确认**：每项带复选框（默认不勾），删除必须勾选 + 二次弹窗确认，
  且**只移入回收站**（代码内没有任何 `rmtree` / `os.remove`）。
- **导出清单**：一键导出 CSV（路径 / 页数 / 相似度），方便存档核对。

### 🔒 隐私

| 承诺 | 落实方式 |
| --- | --- |
| **不联网** | 源码零网络调用（`--selftest` 里有静态检查逐文件核验） |
| **不上传任何数据** | 图片、特征、路径只在本机内存与本地 SQLite 里 |
| **压缩包不落地解压** | 图片在内存中读取解码，用完即弃 |
| **不做 OCR** | 只用分镜图像特征（灰度墨迹分布 + 感知哈希） |
| **绝不自动删除** | 人工勾选 + 二次确认，且只移入回收站 |

### 📊 识别效果（21 项真实语料实测）

```
扫描完成：21 本，共 140 页特征，异常 1 本，耗时 6.8 秒
重复组 4 组，涉及 9 本，可回收 11.33 MB

[组] 1.000  海贼王 第10卷.zip ｜ (改名副本).zip ｜ (重打包).7z      匹配 7/7 页
[组] 0.791  火影忍者 第01卷 [官方DL].7z ｜ [自制扫描].zip          匹配 5/7 页  ← 核心需求
[组] 1.000  进击的巨人 第05卷(原始).zip ｜ (webp低清).zip           匹配 7/7 页
[组] 0.842  死神 第03卷 ｜ 死神 第03卷 (DL文件夹)                   匹配 7/7 页
```

* 6 本「无关漫画」与「孤独的一本」**零误并**；
* 「某系列 第01/02/03 卷」（三卷各自共享 2 页系列页）**没有被连环并成一串**；
* 故意造的损坏 zip 与坏图：只记日志跳过，程序不崩。

### 🧪 一个反直觉的标定结论

**裁剪越「准」，查重结果越差。**

| 裁剪方式 | 同页中位相似度 | 异页最高分 | 多页投票（K=3） |
| --- | --- | --- | --- |
| **只去扫描仪盖板黑边**（最终采用） | **0.814** | 0.497 | 同书 3/3 命中、跨书 0/6 误报 |
| 盖板 + 墨迹尺度归一 | 0.482 | 0.787 | 全部失效 |
| 盖板 + 高频边缘逐边硬裁 | 0.358 | 0.714 | 全部失效 |

根因：自适应阈值型裁剪在「扫描版（噪点多）」和「官方 DL 版（干净）」上裁掉的量必然不同，
反而引入了**相对尺度差** —— 而平移搜索修不了尺度。这条路试了 5 版全部失败。

解法：不再追求完美裁剪，只做尺度归一，让度量自己把残余错位搜出来
（`matchTemplate` 归一化相关，搜索 ±12.5% 幅面）。

### ✅ 验证

`--selftest` 23 项、`--verify` 3 项、界面回归 37 项，全部通过（可复现）。

### 🔒 校验（SHA256）

```
{sha_lines}
```

### 📄 许可

MIT License · Copyright (c) 2026 seanfan
"""


def set_repo_meta(token: str) -> int:
    """设置仓库简介与话题（幂等，可重复执行）。

    ⚠ 话题必须走**专用接口** ``PUT /repos/{o}/{r}/topics``：
    在 ``PATCH /repos/{o}/{r}`` 里捎带 ``topics`` 会返回 200 但**被静默忽略**
    （实测该请求只改掉了 description，topics 回读仍是空数组）。
    """
    status, res = api(f"/repos/{OWNER}/{REPO}", "PATCH",
                      json.dumps({"description": REPO_DESC,
                                  "homepage": ""}).encode(), token)
    if status != 200:
        print(f"设置仓库简介失败（HTTP {status}）：{res}")
        return 1
    print(f"仓库简介已设置：{res.get('description')}")

    status, res2 = api(f"/repos/{OWNER}/{REPO}/topics", "PUT",
                       json.dumps({"names": REPO_TOPICS}).encode(), token)
    if status != 200:
        print(f"设置话题失败（HTTP {status}）：{res2}")
        return 1
    got = res2.get("names") or []
    print(f"话题已设置（{len(got)} 个）：{', '.join(got)}")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--version", default=DEFAULT_VERSION)
    ap.add_argument("--meta", action="store_true", help="只设置仓库简介与话题")
    ap.add_argument("--no-upload", action="store_true", help="只建 release，不上传附件")
    args = ap.parse_args()

    version = args.version
    token = get_token()
    if not token:
        print("未从本机 Git 凭据管理器取到 GitHub 凭据，无法调用 API。")
        print(f"可改为手动发布：在仓库 Releases 页面选择 tag {version}，把 dist/ 里的文件拖进去。")
        return 2

    if args.meta:
        return set_repo_meta(token)

    # ---- 收集附件
    # 只发文件夹版的 zip：它比单文件 exe 更小（105MB vs 107MB）、启动更快，
    # 而且不含自解压行为、不容易被杀软误报。要单文件 exe 自行跑 打包exe.bat。
    assets: list = []
    for name, note in (
        (f"ComicDedupTool-{version}-win64.zip",
         "解压后双击里面的 `ComicDedupTool.exe` 即可，启动是瞬时的"),
    ):
        p = ROOT / "dist" / name
        if p.exists():
            assets.append((p, note))
        else:
            print(f"提示：没找到 {p}（跳过）。先双击 打包exe.bat 打包。")
    if not assets and not args.no_upload:
        print("dist/ 里没有可发布的产物，退出。")
        return 1

    body = build_body(version, assets)
    release_name = f"{version} — 漫画查重 ComicDedup（纯本地离线）"

    # ---- 1) 已存在同 tag 的 release 就复用并更新，否则新建
    status, rel = api(f"/repos/{OWNER}/{REPO}/releases/tags/{version}", token=token)
    if status == 200:
        print(f"已存在 release {version}，更新说明…")
        status, rel = api(f"/repos/{OWNER}/{REPO}/releases/{rel['id']}", "PATCH",
                          json.dumps({"name": release_name, "body": body}).encode(), token)
    else:
        print(f"创建 release {version} …")
        status, rel = api(f"/repos/{OWNER}/{REPO}/releases", "POST",
                          json.dumps({"tag_name": version, "target_commitish": "main",
                                      "name": release_name, "body": body,
                                      "draft": False, "prerelease": False}).encode(), token)
    if status not in (200, 201):
        print(f"创建/更新 release 失败（HTTP {status}）：{rel}")
        return 3
    rid = rel["id"]
    print(f"  成功：{rel['html_url']}")

    # ---- 2) 清掉同名旧附件，避免重复
    status, old = api(f"/repos/{OWNER}/{REPO}/releases/{rid}/assets", token=token)
    existing = {a["name"]: a["id"] for a in old} if status == 200 else {}
    for path, _note in assets:
        if path.name in existing:
            print(f"  删除旧附件 {path.name} …")
            api(f"/repos/{OWNER}/{REPO}/releases/assets/{existing[path.name]}",
                "DELETE", token=token)

    # ---- 3) 上传附件（大文件给更长的超时）
    if not args.no_upload:
        upload_base = rel["upload_url"].split("{")[0]
        for path, _note in assets:
            size = path.stat().st_size
            ctype = CONTENT_TYPES.get(path.suffix.lower(), "application/octet-stream")
            print(f"上传 {path.name}（{size / 1048576:.1f} MB）…")
            upload_url = upload_base + f"?name={path.name}"
            timeout = 1800 if size > 50 * 1048576 else 600
            status, asset = api(upload_url, "POST", path.read_bytes(), token, ctype,
                                timeout=timeout)
            if status not in (200, 201):
                print(f"  上传失败（HTTP {status}）：{asset}")
                return 4
            print(f"  成功：{asset['browser_download_url']}")

    # ---- 4) 回读服务端 sha256 与本地比对（不用下载附件）
    status, rel2 = api(f"/repos/{OWNER}/{REPO}/releases/{rid}", token=token)
    if status == 200:
        print("\n服务端校验：")
        for a in rel2.get("assets", []):
            local = ROOT / "dist" / a["name"]
            local_sha = (hashlib.sha256(local.read_bytes()).hexdigest()
                         if local.exists() else "")
            got = (a.get("digest") or "").replace("sha256:", "")
            mark = "一致 OK" if got and got == local_sha else "不一致 !!"
            print(f"  {a['name']:26s} {a['size'] / 1048576:6.1f} MB  {mark}")

    print("\n发布完成 OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
