from pathlib import Path

# Remove corrupted filenames
for p in Path('.').glob('*.bat'):
    if any(ord(c) > 127 and ('\ufffd' in p.name or '?' in p.name) for c in p.name):
        try:
            p.unlink()
        except Exception:
            pass

start_protocol = """@echo off
chcp 65001 >nul
title ChatGPT 极速协议注册控制台 (端口 8889)
cd /d "%~dp0"

echo ======================================================================
echo           ChatGPT 极速协议版 (纯 HTTP/2 + Sentinel 逆向 PoW)
echo ======================================================================
echo.

if not exist "gpt-auto-register\\.venv\\Scripts\\python.exe" (
    echo [错误] 未检测到 Python 虚拟环境: gpt-auto-register\\.venv
    pause
    exit /b 1
)

echo [1/2] 正在启动 ChatGPT 极速协议注册服务 (端口 8889)...
start "ChatGPT 极速协议版 (端口 8889)" /d "%~dp0gpt-protocol-register" "%~dp0gpt-auto-register\\.venv\\Scripts\\python.exe" server.py

echo [2/2] 正在打开控制台界面...
ping -n 3 127.0.0.1 >nul
start http://localhost:8889

echo.
echo ======================================================================
echo 极速协议注册服务已启动！
echo.
echo [Web控制台]:   http://localhost:8889
echo [核心特性]:    纯协议高并发注册、免浏览器渲染、Sentinel PoW 自动计算
echo [数据存储]:    共享主数据库 registered_accounts.txt
echo ======================================================================
echo.
pause
"""

start_all = """@echo off
chcp 65001 >nul
title ChatGPT 一体化注册与管理套件
cd /d "%~dp0"

echo ======================================================================
echo                ChatGPT 一体化批量注册与 API 网关服务
echo ======================================================================
echo.

if not exist "gpt-auto-register\\.venv\\Scripts\\python.exe" (
    echo [错误] 未检测到 gpt-auto-register 虚拟环境
    pause
    exit /b 1
)

if not exist "chat2api\\.venv\\Scripts\\python.exe" (
    echo [错误] 未检测到 chat2api 虚拟环境
    pause
    exit /b 1
)

echo [1/3] 正在启动 Chat2Api (OpenAI API 格式转换引擎，端口 5005)...
start "Chat2Api 服务 (端口 5005)" /d "%~dp0chat2api" "%~dp0chat2api\\.venv\\Scripts\\python.exe" app.py

echo [2/3] 正在启动 GPT 自动化注册控制台 (端口 8888)...
start "GPT 自动化注册控制台 (端口 8888)" /d "%~dp0gpt-auto-register" "%~dp0gpt-auto-register\\.venv\\Scripts\\python.exe" server.py

echo [3/3] 正在打开控制台页面...
ping -n 3 127.0.0.1 >nul
start http://localhost:8888

echo.
echo ======================================================================
echo 一体化服务已全部启动！
echo.
echo [Web控制台] (账号管理/批量注册/在线检测):  http://localhost:8888
echo [OpenAI API] (客户端接入统一端口):        http://localhost:8888/v1/chat/completions
echo [OpenAI API] (底层直连端口 5005):         http://127.0.0.1:5005/v1/chat/completions
echo [API 文档与测试]:                         http://127.0.0.1:5005/docs
echo.
echo 提示：NextChat / Chatbox 客户端请设置 Base URL 为 http://localhost:8888/v1 即可
echo ======================================================================
echo.
pause
"""

start_browser = """@echo off
chcp 65001 >nul
title ChatGPT 浏览器自动化注册控制台 (端口 8888)
cd /d "%~dp0"

echo ======================================================================
echo              ChatGPT 浏览器自动化注册版 (端口 8888)
echo ======================================================================
echo.

if not exist "gpt-auto-register\\.venv\\Scripts\\python.exe" (
    echo [错误] 未检测到 Python 虚拟环境: gpt-auto-register\\.venv
    pause
    exit /b 1
)

echo [1/2] 正在启动 ChatGPT 浏览器自动化注册服务 (端口 8888)...
start "ChatGPT 浏览器版 (端口 8888)" /d "%~dp0gpt-auto-register" "%~dp0gpt-auto-register\\.venv\\Scripts\\python.exe" server.py

echo [2/2] 正在打开控制台界面...
ping -n 3 127.0.0.1 >nul
start http://localhost:8888

echo.
echo ======================================================================
echo 浏览器自动化服务已启动！
echo [Web控制台]: http://localhost:8888
echo ======================================================================
echo.
pause
"""

Path('start_protocol.bat').write_text(start_protocol, encoding='utf-8')
Path('start_all.bat').write_text(start_all, encoding='utf-8')
Path('start_browser.bat').write_text(start_browser, encoding='utf-8')
Path('一键启动极速协议版.bat').write_text(start_protocol, encoding='utf-8')
Path('一键启动全部服务.bat').write_text(start_all, encoding='utf-8')
print("Successfully generated all .bat files with UTF-8 and chcp 65001!")
