Set-Location $PSScriptRoot
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host "              Chat2Api 服务启动中..." -ForegroundColor Cyan
Write-Host "========================================================" -ForegroundColor Cyan
Write-Host ""

$pythonPath = Join-Path $PSScriptRoot ".venv\Scripts\python.exe"
if (-not (Test-Path $pythonPath)) {
    Write-Host "[错误] 未检测到 .venv 虚拟环境！" -ForegroundColor Red
    pause
    exit 1
}

Write-Host "[提示] 正在使用本地虚拟环境启动服务..." -ForegroundColor Green
Write-Host "[提示] 默认服务地址: http://127.0.0.1:5005" -ForegroundColor Yellow
Write-Host "[提示] API 接口地址: http://127.0.0.1:5005/v1/chat/completions" -ForegroundColor Yellow
Write-Host "[提示] Tokens 管理页: http://127.0.0.1:5005/tokens" -ForegroundColor Yellow
Write-Host "[提示] 接口文档地址: http://127.0.0.1:5005/docs" -ForegroundColor Yellow
Write-Host ""

& $pythonPath app.py
