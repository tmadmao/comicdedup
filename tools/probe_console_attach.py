"""探测：GUI 子系统 exe 的控制台输出去哪了。

目的：确认「窗口版 exe 能不能当命令行版用」。
三种启动方式：
  A 由已有控制台的进程直接启动  → 句柄被继承，应该有输出
  B 分离式启动（近似资源管理器双击）→ 没有控制台，应该零输出
"""
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
src = ROOT / "dist" / "ComicDedupTool"
if not src.exists():
    print(f"找不到 {src}，先执行 onedir 打包（双击 打包exe.bat）")
    sys.exit(2)

dst = Path(os.environ["LOCALAPPDATA"]) / "Temp" / f"ctltest_{int(time.time())}"
if dst.exists():
    shutil.rmtree(dst, ignore_errors=True)
shutil.copytree(src, dst)
exe = dst / "ComicDedupTool.exe"
env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}

print(f"被测 exe：{exe.name}（GUI 子系统）\n")

r1 = subprocess.run([str(exe), "--selftest"], capture_output=True, text=True,
                    timeout=240, env=env)
ok1 = "自检结束" in r1.stdout
print(f"  [A] 从已有控制台启动：rc={r1.returncode}  stdout={len(r1.stdout)} 字节  "
      f"读到自检结果={'是' if ok1 else '否'}")

DETACHED_PROCESS = 0x00000008
r2 = subprocess.run([str(exe), "--selftest"], capture_output=True, text=True,
                    timeout=240, env=env, creationflags=DETACHED_PROCESS)
ok2 = "自检结束" in r2.stdout
print(f"  [B] 分离启动(近似双击)：rc={r2.returncode}  stdout={len(r2.stdout)} 字节  "
      f"读到自检结果={'是' if ok2 else '否'}")

print()
if ok1 and not ok2:
    print("结论：窗口版 exe 只有在「从已有控制台启动」时输出才看得到；")
    print("      双击（无控制台）时静默运行 —— 所以常规用法下它完全够用，")
    print("      不需要再单独发一个命令行版。")
elif ok1 and ok2:
    print("结论：两种方式都能拿到输出。")
else:
    print("结论：都拿不到输出，需要单独的命令行版。")

# 用完就删 —— 这个副本解开有 260MB，别留在 %TEMP% 里
shutil.rmtree(dst, ignore_errors=True)
