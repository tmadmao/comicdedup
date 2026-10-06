@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ============================================================
echo   漫画查重 - 启动
echo ============================================================
echo.
echo   本程序纯本地离线运行：
echo     - 不联网、不上传任何图片或特征数据
echo     - 不做 OCR，只做图片分镜图像特征比对
echo     - 压缩包内图片只在内存里读取，不会解压到磁盘
echo     - 不会自动删除任何文件；删除必须人工勾选并二次确认
echo.

set "PYCMD=python"
py -3 --version >nul 2>nul && set "PYCMD=py -3"
%PYCMD% --version >nul 2>nul
if errorlevel 1 goto NOPY

%PYCMD% -c "import cv2,numpy,PIL" >nul 2>nul
if errorlevel 1 (
  echo [提示] 缺少运行依赖，先帮你安装一次 ...
  %PYCMD% -m pip install opencv-python numpy pillow send2trash
  %PYCMD% -m pip install PyQt5 || %PYCMD% -m pip install PySide6
)

echo 正在启动图形界面 ...
%PYCMD% comic_dedup.py
if errorlevel 3 (
  echo.
  echo [提示] 界面没起来（可能没装 Qt）。可以直接用命令行模式：
  echo        %PYCMD% comic_dedup.py --scan "你的漫画目录" --csv 重复清单.csv
  pause
)
exit /b 0

:NOPY
echo [错误] 没检测到 Python，请先双击「安装依赖.bat」
pause
exit /b 1
