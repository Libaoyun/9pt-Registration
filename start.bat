@echo off
chcp 65001 >nul
title Chat2Api Server
cd /d "%~dp0"

echo ========================================================
echo               Chat2Api 服务启动中...
echo ========================================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未检测到 .venv 虚拟环境！
    pause
    exit /b 1
)

echo [提示] 正在使用本地虚拟环境启动服务...
echo [提示] 默认服务地址: http://127.0.0.1:5005
echo [提示] API 接口地址: http://127.0.0.1:5005/v1/chat/completions
echo [提示] Tokens 管理页: http://127.0.0.1:5005/tokens
echo [提示] 接口文档地址: http://127.0.0.1:5005/docs
echo.

".venv\Scripts\python.exe" app.py
pause
