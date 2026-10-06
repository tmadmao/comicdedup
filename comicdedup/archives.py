"""压缩包内存读取 —— 漫画单行本的三种载体：zip / rar / 7z，以及图片文件夹。

设计要点
--------
* **绝不落地解压**：所有图片都在内存里解码。压缩包内容不进临时目录、不写磁盘。
* **后端链 + 优雅降级**：能用 Python 原生/纯 Python 库就用库（快、无外部依赖），
  否则自动找本机的解压程序（7-Zip / WinRAR / Windows 自带的 bsdtar/libarchive），
  全都找不到就记日志跳过这本，程序继续跑。
* **按扩展名不可靠**：有些 .zip 其实是 rar（或反之），所以读**文件头魔数**判断真实格式。
* **一次进程调用读完一本**：外部解压程序每个只调一次（把要抽的页一次抽完再按大小切开），
  而不是每张图调一次 —— 3000 本 × 每本 10 页 = 3 万次进程调用会慢到无法接受。

后端优先级
----------
=========  ==========================================================
zip        stdlib ``zipfile``（原生、支持随机访问、能处理中文文件名编码）
rar        ``rarfile`` → ``UnRAR.exe`` → ``7z.exe`` → ``bsdtar``
7z         ``py7zr`` → ``7z.exe``/``7za.exe`` → ``bsdtar``
=========  ==========================================================
"""

from __future__ import annotations

import io
import os
import re
import shutil
import subprocess
import sys
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional, Sequence

ZIP_EXT = {".zip", ".cbz"}
RAR_EXT = {".rar", ".cbr"}
SEVEN_EXT = {".7z", ".cb7"}
ARCHIVE_EXT = ZIP_EXT | RAR_EXT | SEVEN_EXT
IMG_EXT = {".jpg", ".jpeg", ".jpe", ".png", ".webp", ".bmp", ".gif", ".tif", ".tiff", ".avif"}
DOC_EXT = {".xml", ".txt", ".nfo", ".json", ".db", ".url", ".html", ".htm", ".md", ".pdf"}
SKIP_DIR_PREFIX = ("__MACOSX/", ".DS_Store", "__MACOSX\\")

MAGIC_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
MAGIC_RAR = (b"Rar!\x1a\x07\x00", b"Rar!\x1a\x07\x01\x00")
MAGIC_7Z = (b"7z\xbc\xaf\x27\x1c",)

EXTERNAL_TIMEOUT = 180


# ------------------------------------------------------------------ 工具


def _natural_key(name: str):
    """自然排序键：让 1.jpg < 2.jpg < 10.jpg（漫画页序不能按字典序排）。"""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    parts = re.split(r"(\d+)", base.lower())
    return [int(p) if p.isdigit() else p for p in parts]


def is_image_name(name: str) -> bool:
    n = name.replace("\\", "/")
    if n.startswith(SKIP_DIR_PREFIX) or n.rsplit("/", 1)[-1].startswith("."):
        return False
    return Path(n).suffix.lower() in IMG_EXT


def sniff_format(path: Path) -> Optional[str]:
    """按文件头魔数判断真实格式，返回 'zip' / 'rar' / '7z' / None。"""
    try:
        with open(path, "rb") as f:
            head = f.read(8)
    except Exception:
        return None
    for m in MAGIC_ZIP:
        if head.startswith(m):
            return "zip"
    for m in MAGIC_RAR:
        if head.startswith(m):
            return "rar"
    for m in MAGIC_7Z:
        if head.startswith(m):
            return "7z"
    return None


def find_tool(names: Sequence[str], extra_dirs: Sequence[str] = ()) -> Optional[str]:
    """在 PATH 与常见安装目录里找可执行文件。"""
    for n in names:
        p = shutil.which(n)
        if p:
            return p
    for d in list(extra_dirs) + _default_tool_dirs():
        for n in names:
            cand = Path(d) / (n + ".exe")
            if cand.exists():
                return str(cand)
            cand = Path(d) / n
            if cand.exists():
                return str(cand)
    return None


def _default_tool_dirs() -> list:
    dirs = []
    for env in ("ProgramFiles", "ProgramFiles(x86)", "ProgramW6432"):
        base = os.environ.get(env)
        if base:
            dirs += [str(Path(base) / "7-Zip"), str(Path(base) / "7-Zip-Zstandard"),
                     str(Path(base) / "WinRAR")]
    windir = os.environ.get("WINDIR") or r"C:\Windows"
    dirs.append(str(Path(windir) / "System32"))     # Windows 自带 bsdtar(libarchive)
    # 程序自身目录（允许用户把 7z.exe 丢在 exe 旁边，便携使用）
    dirs.append(str(Path(sys.executable).resolve().parent))
    dirs.append(str(Path(__file__).resolve().parent.parent))
    return dirs


def _run(cmd: list, **kw) -> subprocess.CompletedProcess:
    kw.setdefault("stdout", subprocess.PIPE)
    kw.setdefault("stderr", subprocess.PIPE)
    kw.setdefault("timeout", EXTERNAL_TIMEOUT)
    creationflags = 0x08000000 if os.name == "nt" else 0   # CREATE_NO_WINDOW
    return subprocess.run(cmd, creationflags=creationflags, **kw)


# ------------------------------------------------------------------ 后端探测

_TOOLS: dict = {}


def available_backends() -> dict:
    """探测本机可用的后端（结果缓存）。"""
    if _TOOLS:
        return _TOOLS
    _TOOLS["sevenzip"] = find_tool(["7z", "7za", "7zz"])
    _TOOLS["unrar"] = find_tool(["UnRAR", "unrar"])
    _TOOLS["rar"] = find_tool(["Rar", "rar"])
    # Windows 自带 C://Windows//System32//tar.exe 就是 bsdtar(libarchive)，
    # 优先直接探测它 —— 否则 PATH 里的 GNU tar（Git Bash 自带）会先被找到，
    # 而 GNU tar 读不了 7z/rar。
    bsdtar = None
    windir = os.environ.get("WINDIR") or r"C://Windows"
    for cand in (Path(windir) / "System32" / "tar.exe", Path(windir) / "System32" / "bsdtar.exe"):
        if cand.exists():
            bsdtar = str(cand)
            break
    if not bsdtar:
        for name in ("bsdtar", "tar"):
            p2 = shutil.which(name)
            if p2:
                bsdtar = p2
                break
    _TOOLS["bsdtar"] = bsdtar
    for mod in ("py7zr", "rarfile"):
        try:
            __import__(mod)
            _TOOLS[mod] = True
        except Exception:
            _TOOLS[mod] = False
    # Windows 自带的 tar.exe 是 bsdtar；GNU tar 读不了 7z/rar，所以验证一下
    if _TOOLS.get("bsdtar"):
        try:
            r = _run([_TOOLS["bsdtar"], "--version"], timeout=10)
            out = (r.stdout or b"").decode("utf-8", "replace").lower()
            if "bsdtar" not in out and "libarchive" not in out:
                _TOOLS["bsdtar"] = None
        except Exception:
            _TOOLS["bsdtar"] = None
    return _TOOLS


# ------------------------------------------------------------------ 数据模型


@dataclass
class BookEntry:
    """一本单行本的元信息（不含图片数据）。"""

    path: Path
    kind: str                 # "archive" / "folder"
    fmt: str                  # zip / rar / 7z / folder
    size: int                 # 字节数（文件夹=所有图片字节和）
    mtime: float
    pages: int                # 图片总数
    names: list = field(default_factory=list)   # 图片成员名（已按页序排好）
    error: str = ""


@dataclass
class BookImage:
    """从压缩包/文件夹里读到内存的一张图。"""

    name: str
    index: int
    data: bytes


class ArchiveError(Exception):
    pass


# ------------------------------------------------------------------ zip 后端


def _decode_zip_name(info: zipfile.ZipInfo) -> str:
    """zip 里的中文文件名编码很乱（CP437 / GBK / UTF-8 都常见），尽量还原。

    ``zipfile`` 在 flag bit 11 未置位时会用 CP437 解出乱码；这里按「CP437 反推字节 →
    依次尝试 UTF-8 / GBK」还原成可读名字，只用于显示与排序，不影响读取数据。
    """
    if info.flag_bits & 0x800:
        return info.filename
    try:
        raw = info.filename.encode("cp437")
    except Exception:
        return info.filename
    for enc in ("utf-8", "gbk", "big5", "shift_jis"):
        try:
            return raw.decode(enc)
        except Exception:
            continue
    return info.filename


class ZipBackend:
    fmt = "zip"

    def __init__(self, path: Path):
        try:
            self.zf = zipfile.ZipFile(path)
        except Exception as e:
            raise ArchiveError(f"无法打开 zip：{e}") from e

    def list_images(self) -> list:
        out = []
        for info in self.zf.infolist():
            if info.is_dir():
                continue
            name = _decode_zip_name(info)
            if is_image_name(name):
                out.append((name, info.file_size))
        out.sort(key=lambda t: _natural_key(t[0]))
        return out

    def read(self, name: str) -> bytes:
        # 名字可能被上面的编码还原过，所以要按原始名字反查
        for info in self.zf.infolist():
            if info.is_dir():
                continue
            if _decode_zip_name(info) == name or info.filename == name:
                return self.zf.read(info)
        raise ArchiveError(f"成员不存在：{name}")

    def read_many(self, names: Sequence[str]) -> dict:
        want = set(names)
        out = {}
        for info in self.zf.infolist():
            if info.is_dir():
                continue
            n = _decode_zip_name(info)
            if n in want:
                out[n] = self.zf.read(info)
        return out

    def close(self):
        try:
            self.zf.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 外部程序后端


class ExternalBackend:
    """用外部解压程序一次性把需要的成员抽到内存（不落地）。

    命令选择：
      * 7z / 7za：``7z x -so -y -- archive member...``（-so = 输出到 stdout）
      * UnRAR：  ``unrar p -inul -p- -- archive member...``（p = 打印到 stdout）
      * bsdtar： ``tar -xOf archive member...``（-O = 输出到 stdout）
    多个成员会**首尾相接**输出，所以按清单里的顺序与大小切开。
    """

    def __init__(self, path: Path, fmt: str, tool_key: str, tool: str):
        self.path = path
        self.fmt = fmt
        self.tool_key = tool_key
        self.tool = tool
        self._sizes: dict = {}

    def _list_cmd(self) -> list:
        if self.tool_key == "sevenzip":
            return [self.tool, "l", "-slt", "-ba", "--", str(self.path)]
        if self.tool_key == "unrar":
            return [self.tool, "l", "-c-", "--", str(self.path)]
        # bsdtar 必须用 -tvf（详细列表）才带成员大小 —— 没有大小就无法把
        # 「一次调用抽多个成员」的首尾相接输出切开
        return [self.tool, "-tvf", str(self.path)]

    def list_images(self) -> list:
        r = _run(self._list_cmd())
        out = (r.stdout or b"").decode("utf-8", "replace")
        err = (r.stderr or b"").decode("utf-8", "replace")
        if r.returncode not in (0, 1) and not out.strip():
            raise ArchiveError(f"列目录失败：{err.strip()[:200]}")
        names: list = []
        if self.tool_key == "sevenzip":
            cur = {}
            for line in out.splitlines():
                line = line.strip()
                if line.startswith("Path = "):
                    cur = {"name": line[7:]}
                elif line.startswith("Size = "):
                    try:
                        cur["size"] = int(line[7:])
                    except ValueError:
                        cur["size"] = 0
                elif line.startswith("Attributes = "):
                    cur["dir"] = "D" in line[13:]
                    if "name" in cur and not cur.get("dir"):
                        if is_image_name(cur["name"]):
                            names.append((cur["name"], int(cur.get("size", 0))))
                        self._sizes[cur["name"]] = int(cur.get("size", 0))
                    cur = {}
            # 7z 的 -slt 里 "Path" 也会出现归档自身，跳过不含图片的
        elif self.tool_key == "unrar":
            # UnRAR `l -c-` 的列格式（属性在最左、文件名在最右）：
            #     ..A....    646135  2026-10-06 21:40  001.jpg
            # ⚠ 不能用 parts[0] 当文件名 —— 那是属性串（实测踩过）。
            # 文件名可能含空格，所以用正则把前四列吃掉，剩下整段就是文件名。
            pat = re.compile(r"^\s*\S+\s+(\d+)\s+\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}\s+(.+?)\s*$")
            for line in out.splitlines():
                m = pat.match(line)
                if m and is_image_name(m.group(2)):
                    names.append((m.group(2), int(m.group(1))))
        else:  # bsdtar -tvf：-rw-r--r--  0 0 0  646135 Oct 06 21:40 001.jpg
            pat = re.compile(r"^[-d][rwxstST-]{9}\+?\s+(?:\d+\s+){3}(\d+)\s+\S+\s+\S+\s+\S*\s*(.+?)\s*$")
            for line in out.splitlines():
                m = pat.match(line)
                if m and is_image_name(m.group(2)):
                    names.append((m.group(2), int(m.group(1))))
        names.sort(key=lambda t: _natural_key(t[0]))
        for n, s in names:
            self._sizes.setdefault(n, s)
        return names

    def _extract_cmd(self, names: Sequence[str]) -> list:
        if self.tool_key == "sevenzip":
            return [self.tool, "x", "-so", "-y", "-bd", "--", str(self.path), *names]
        if self.tool_key == "unrar":
            return [self.tool, "p", "-inul", "-p-", "--", str(self.path), *names]
        return [self.tool, "-xOf", str(self.path), *names]

    def read_many(self, names: Sequence[str]) -> dict:
        if not names:
            return {}
        # 先把大小问清楚（7z 的 -slt 已给出；其他情况按需补一次）
        r = _run(self._extract_cmd(names), stdout=subprocess.PIPE)
        blob = r.stdout or b""
        if not blob:
            err = (r.stderr or b"").decode("utf-8", "replace")
            raise ArchiveError(f"抽取失败：{err.strip()[:200]}")
        out = {}
        pos = 0
        for n in names:
            sz = int(self._sizes.get(n, 0))
            if sz <= 0 or pos + sz > len(blob):
                out[n] = None            # 大小未知 → 交给调用方逐张回退
                continue
            out[n] = blob[pos:pos + sz]
            pos += sz
        return out

    def read(self, name: str) -> bytes:
        got = self.read_many([name]).get(name)
        if not got:
            raise ArchiveError(f"抽取失败或大小为 0：{name}")
        return got

    def close(self):
        pass


# ------------------------------------------------------------------ 统一入口


def open_book(path: Path) -> object:
    """按文件头魔数挑后端打开一本。失败抛 ArchiveError。"""
    fmt = sniff_format(path)
    if fmt is None:
        ext = path.suffix.lower()
        fmt = "zip" if ext in ZIP_EXT else "rar" if ext in RAR_EXT else \
            "7z" if ext in SEVEN_EXT else None
    if fmt is None:
        raise ArchiveError("无法识别的压缩包格式")
    if fmt == "zip":
        return ZipBackend(path)
    tb = available_backends()
    if fmt == "7z" and tb.get("py7zr"):
        try:
            return _Py7zrBackend(path)
        except Exception:
            pass
    if fmt == "rar" and tb.get("rarfile"):
        try:
            return _RarfileBackend(path)
        except Exception:
            pass
    # zip 之外都优先用 7z：它的 -slt 详细列表同时给出成员名与大小，
    # 而「一次调用抽多个成员」必须靠大小把首尾相接的输出切开；
    # UnRAR 与 bsdtar 作为兜底（它们的列表格式没有 7z 规整）。
    order = (["sevenzip", "bsdtar"] if fmt == "7z" else ["sevenzip", "unrar", "bsdtar"])
    for key in order:
        tool = tb.get(key)
        if tool:
            try:
                be = ExternalBackend(path, fmt, key, tool)
                be.list_images()
                return be
            except Exception:
                continue
    raise ArchiveError(
        f"没有可用的 {fmt} 解压后端。可安装 7-Zip 或 WinRAR，"
        f"或 pip install {'py7zr' if fmt == '7z' else 'rarfile'}")


class _Py7zrBackend:
    fmt = "7z"

    def __init__(self, path: Path):
        import py7zr  # type: ignore
        self.z = py7zr.SevenZipFile(str(path), mode="r")

    def list_images(self) -> list:
        out = []
        for n, info in zip(self.z.getnames(), self.z.list()):
            if info.is_directory:
                continue
            if is_image_name(n):
                out.append((n, int(getattr(info, "uncompressed", 0) or 0)))
        out.sort(key=lambda t: _natural_key(t[0]))
        return out

    def _reset(self):
        try:
            self.z.reset()
        except Exception:
            pass

    def read_many(self, names: Sequence[str]) -> dict:
        self._reset()
        out = {}
        for n, bio in (self.z.read(targets=list(names)) or {}).items():
            if bio is not None:
                out[n] = bio.read() if hasattr(bio, "read") else bytes(bio)
        return out

    def read(self, name: str) -> bytes:
        d = self.read_many([name])
        if name not in d:
            raise ArchiveError(f"成员不存在：{name}")
        return d[name]

    def close(self):
        try:
            self.z.close()
        except Exception:
            pass


class _RarfileBackend:
    fmt = "rar"

    def __init__(self, path: Path):
        import rarfile  # type: ignore
        self.path = path
        self.rf = rarfile.RarFile(str(path))

    def list_images(self) -> list:
        out = [(i.filename, int(getattr(i, "file_size", 0) or 0))
               for i in self.rf.infolist() if not i.isdir() and is_image_name(i.filename)]
        out.sort(key=lambda t: _natural_key(t[0]))
        return out

    def read_many(self, names: Sequence[str]) -> dict:
        out = {}
        for n in names:
            try:
                out[n] = self.rf.read(n)
            except Exception:
                pass
        return out

    def read(self, name: str) -> bytes:
        return self.rf.read(name)

    def close(self):
        try:
            self.rf.close()
        except Exception:
            pass


# ------------------------------------------------------------------ 扫描发现


def scan_books(root: Path, progress=None, should_stop=None) -> list:
    """递归扫描根目录，识别「压缩包」与「漫画图片文件夹」作为单行本。

    一个 ``BookEntry`` = 一本单行本。规则：
      * 压缩包（zip/rar/7z/cbz/cbr/cb7）：直接算一本；其内部子目录不再单独成书。
      * 图片文件夹：**直接**含图片文件的目录算一本；若其子目录也直接含图片，
        则各自成一本（很多合集是「系列/卷/页.jpg」结构）。
    """
    root = Path(root)
    books: list = []
    seen_dirs: set = set()

    for dirpath, dirnames, filenames in os.walk(root):
        if should_stop and should_stop():
            break
        d = Path(dirpath)
        dirnames[:] = [x for x in dirnames if not x.startswith(".")
                       and x not in ("_漫画查重回收站", "$RECYCLE.BIN", "System Volume Information")]
        # 1) 压缩包
        for fn in filenames:
            if Path(fn).suffix.lower() in ARCHIVE_EXT:
                books.append(BookEntry(path=d / fn, kind="archive",
                                       fmt=Path(fn).suffix.lower().lstrip("."),
                                       size=0, mtime=0.0, pages=0))
        # 2) 图片文件夹（只看「本层直接含图」的目录）
        imgs = [fn for fn in filenames if Path(fn).suffix.lower() in IMG_EXT]
        if imgs and str(d) not in seen_dirs:
            seen_dirs.add(str(d))
            total = 0
            for fn in imgs:
                try:
                    total += (d / fn).stat().st_size
                except OSError:
                    pass
            books.append(BookEntry(path=d, kind="folder", fmt="folder",
                                   size=total, mtime=0.0, pages=len(imgs)))
        if progress:
            progress(len(books), dirpath)

    # 过滤掉「父目录和子目录都被当成书」的重复情况：父目录若其图片全在子目录里就不算
    final = []
    for b in books:
        if b.kind == "folder":
            sub_imgs = sum(1 for dp, _dn, fns in os.walk(b.path)
                           for fn in fns if Path(fn).suffix.lower() in IMG_EXT)
            if sub_imgs == 0:
                continue
        final.append(b)
    return final


def stat_book(b: BookEntry) -> BookEntry:
    """补齐 size / mtime（发现阶段为了快没 stat 压缩包）。"""
    try:
        st = b.path.stat()
        # ⚠ 文件夹不能拿目录自身的大小 —— Windows 上目录 stat 只有 4KB，
        # 会把「所有图片字节和」覆盖掉（实测踩过）。文件夹只取 mtime。
        if b.kind != "folder":
            b.size = st.st_size
        b.mtime = st.st_mtime
    except OSError:
        if b.kind != "folder":
            b.size = 0
        b.mtime = 0.0
    return b
