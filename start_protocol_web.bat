@echo off
chcp 65001 >nul
title ChatGPT 极速协议批量注册面板 (Protocol Edition)
cd /d "%~dp0"

echo ========================================================
echo       ChatGPT 极速协议批量注册面板 (端口 8889)
echo ========================================================
echo.

set PYTHON_EXE=..\gpt-auto-register\.venv\Scripts\python.exe
if not exist "%PYTHON_EXE%" (
    echo [错误] 未检测到 Python 虚拟环境: %PYTHON_EXE%
    pause
    exit /b 1
)

echo [提示] 正在启动极速协议 Web 控制台...
echo [提示] 控制台访问地址: http://localhost:8889
echo [提示] 零浏览器开销，支持高并发，打开浏览器即可操作
echo.

"%PYTHON_EXE%" server.py
pause
