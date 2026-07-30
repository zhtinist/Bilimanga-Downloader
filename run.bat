@echo off
REM Windows 双击启动：在命令行里运行下载器。
REM chcp 65001 切到 UTF-8 代码页，避免中文乱码；首次运行自动建 .venv 装依赖。
chcp 65001 >nul
cd /d "%~dp0"
set PYTHONUTF8=1
set PYTHONIOENCODING=utf-8
python start.py %*
if errorlevel 1 pause
