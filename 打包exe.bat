@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   打包免安装版（PyInstaller）
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

echo [4/5] 打包「文件夹版」到 dist\ComicDedupTool\（推荐），约 1-3 分钟...
%PYCMD% -m PyInstaller --onedir --noconsole --noconfirm --clean ^
  --name ComicDedupTool ^
  --collect-submodules comicdedup ^
  --hidden-import PySide6 ^
  --distpath dist --workpath build --specpath build comic_dedup.py
if errorlevel 1 goto FAIL

echo [5/5] 打成发布用 zip ...
%PYCMD% tools\make_release_zip.py
if errorlevel 1 goto FAIL

echo.
echo ============================================================
echo   打包完成，dist\ 里有两样东西：
echo     dist\ComicDedupTool\                       文件夹版（推荐）
echo     dist\ComicDedupTool-v当前版本-win64.zip    上面这个的压缩包
echo     （zip 的版本号取自 comicdedup\__init__.py 的 __version__，不必手改）
echo.
echo   为什么默认发文件夹版而不是单文件 exe：
echo     1) 单文件每次启动都要把 ~260MB 依赖解压到临时目录，双击后要等 5~10 秒；
echo        文件夹版是解压好的，启动是瞬时的。
echo     2) 单文件的自解压行为容易被杀软启发式误判成木马
echo        （实测 360 报过一次 HEUR/QVM202.0.8C7D.Malware.Gen，属误报）。
echo.
echo   想要单文件 exe 的话，再跑一次：
echo     %PYCMD% -m PyInstaller --onefile --noconsole --noconfirm --clean ^
echo       --name ComicDedupTool-onefile --collect-submodules comicdedup ^
echo       --hidden-import PySide6 --distpath dist --workpath build --specpath build comic_dedup.py
echo.
echo   其它说明：
echo     - exe / 文件夹都可以自由改名，比如改成「漫画查重」
echo     - 7z / rar 的解压仍需装有 7-Zip 或 WinRAR（zip 不需要）
echo       也可以把 7z.exe 放到 exe 同目录，程序会自动识别
echo     - 无控制台打包，双击运行时看不到命令行输出是正常的；
echo       想看 --selftest / --scan 的输出，在 cmd 里 cd 到该目录再执行 exe 即可
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
