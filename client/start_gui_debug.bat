@echo off
chcp 65001 >nul 2>&1
title McLink 桌面版（调试模式）
cd /d "%~dp0"
echo 正在以调试模式启动桌面版，出问题时把下面的内容截图。
echo.
python mclink_gui.py --debug
echo.
echo 已退出。
pause