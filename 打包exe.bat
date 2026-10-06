@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   打包免安装版 exe（PyInstaller）
echo ============================================================
echo.

set "PYCMD=python"
py -3 --version >nul 2>nul && set "PYCMD=py -3"
%PYCMD% --version >nul 2>nul
if errorlevel 1 goto NOPY

echo [1/5] 检查运行依赖...
%PYCMD% -c "import cv2,numpy,PIL" >nul 2>nul
if errorlevel 1 (
  echo       缺少依赖，正在安装...
  %PYCMD% -m pip install -r requirements.txt
  if errorlevel 1 goto FAIL
)

echo [2/5] 检查界面库...
%PYCMD% -c "import PyQt5" >nul 2>nul || %PYCMD% -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  echo       没装界面库，尝试安装 PySide6 ...
  %PYCMD% -m pip install PySide6
  if errorlevel 1 goto FAIL
)

echo [3/5] 检查 PyInstaller...
%PYCMD% -m PyInstaller --version >nul 2>nul
if errorlevel 1 (
  echo       未安装，正在安装 PyInstaller...
  %PYCMD% -m pip install pyinstaller
  if errorlevel 1 goto FAIL
)

echo [4/5] 打包窗口版（双击用，不弹黑框），约 1-3 分钟...
%PYCMD% -m PyInstaller --onefile --noconsole --noconfirm --clean ^
  --name ComicDedupTool ^
  --collect-submodules comicdedup ^
  --hidden-import PySide6 ^
  --distpath dist --workpath build --specpath build comic_dedup.py
if errorlevel 1 goto FAIL

echo [5/5] 打包命令行版（--scan / --selftest 用），约 1-3 分钟...
%PYCMD% -m PyInstaller --onefile --console --noconfirm --clean ^
  --name ComicDedupTool-cli ^
  --collect-submodules comicdedup ^
  --hidden-import PySide6 ^
  --distpath dist --workpath build --specpath build comic_dedup.py
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo   打包完成，产物在 dist\ 里：
echo     dist\ComicDedupTool.exe       窗口版 —— 双击直接开图形界面
echo     dist\ComicDedupTool-cli.exe   命令行版 —— 有控制台输出
echo.
echo   为什么是两个 exe：
echo     窗口版是 GUI 子系统程序，Windows 不给它控制台，
echo     所以它的 --scan / --selftest 是「跑了但什么都看不见」。
echo     要看命令行输出、或者写批处理调用，请用 -cli 那个。
echo.
echo   其它说明：
echo     - exe 可自由改名，比如改成「漫画查重.exe」
echo     - 若杀软误报或启动太慢，改用文件夹模式：
echo       把脚本里的 --onefile 删掉，再运行一次
echo     - 7z / rar 的解压仍需本机装有 7-Zip 或 WinRAR（zip 不需要）
echo       也可以把 7z.exe 放到 exe 同目录，程序会自动识别
echo ============================================================
pause
exit /b 0

:NOPY
echo [错误] 没检测到 Python，请先双击「安装依赖.bat」
pause
exit /b 1

:FAIL
echo.
echo [失败] 请把上面的报错信息发出来
pause
exit /b 1
