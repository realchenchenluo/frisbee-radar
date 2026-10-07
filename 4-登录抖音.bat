@echo off
title 登录抖音
cd /d "%~dp0"

echo ============================================================
echo   登录抖音（抓抖音才需要）
echo ============================================================
echo.
echo   会打开浏览器窗口，请扫码登录。
echo   登录态保存到 data\browser_state\douyin，之后长期复用。
echo   不接触账号密码，只保存登录后的 cookie。
echo ============================================================
echo.
pause

.venv\Scripts\python.exe run.py login --platform douyin

echo.
pause
