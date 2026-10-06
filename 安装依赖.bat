@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   漫画查重 - 安装运行依赖
echo ============================================================
echo.
echo   会安装：界面库(PyQt5 或 PySide6) + 图像处理库 + 回收站支持
echo   全部为本地运行的库，工具本身不联网、不上传任何数据。
echo.

set "PYCMD=python"
py -3 --version >nul 2>nul && set "PYCMD=py -3"
%PYCMD% --version >nul 2>nul
if errorlevel 1 goto NOPY

echo [1/3] 升级 pip ...
%PYCMD% -m pip install --upgrade pip

echo.
echo [2/3] 安装必需依赖（opencv / numpy / pillow / send2trash）...
%PYCMD% -m pip install opencv-python numpy pillow send2trash
if errorlevel 1 goto FAIL

echo.
echo [3/3] 安装界面库（PyQt5 装不上会自动改用 PySide6）...
%PYCMD% -m pip install PyQt5
if errorlevel 1 (
  echo        PyQt5 安装失败，改用 PySide6 ...
  %PYCMD% -m pip install PySide6
  if errorlevel 1 goto QTFAIL
)

echo.
echo ============================================================
echo   依赖安装完成
echo   下一步：双击「运行工具.bat」，或运行  python comic_dedup.py --selftest 自检
echo ============================================================
pause
exit /b 0

:NOPY
echo [错误] 没检测到 Python。
echo        请到 https://www.python.org/downloads/ 安装 Python 3.9 以上版本，
echo        安装时务必勾选「Add Python to PATH」。
pause
exit /b 1

:QTFAIL
echo [失败] 界面库没装上。可以先只用命令行模式：
echo        python comic_dedup.py --scan "D:\漫画" --csv 重复清单.csv
pause
exit /b 1

:FAIL
echo.
echo [失败] 请把上面的报错信息发出来
pause
exit /b 1
