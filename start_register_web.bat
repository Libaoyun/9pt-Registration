@echo off
chcp 65001 >nul
title ChatGPT 批量自动注册面板
cd /d "%~dp0"

echo ========================================================
echo          ChatGPT 批量自动注册面板启动中...
echo ========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未检测到 .venv 虚拟环境！
    pause
    exit /b 1
)

echo [提示] 正在启动 Web 控制台...
echo [提示] 控制台访问地址: http://localhost:8888
echo [提示] 打开浏览器即可点击【启动任务】一键开始批量注册
echo.

".venv\Scripts\python.exe" server.py
pause
