@echo off
title 登录小红书
cd /d "%~dp0"

echo ============================================================
echo   登录小红书（抓小红书才需要）
echo ============================================================
echo.
echo   会打开浏览器窗口，请用小红书 App 扫码。
echo.
echo   二维码有效期很短，过期会自动刷新，日志里会提示。
echo   扫码期间请不要动窗口，一切都由程序自动处理。
echo.
echo   登录态保存到 data\browser_state\xiaohongshu，之后长期复用。
echo   不接触账号密码，只保存登录后的 cookie。
echo ============================================================
echo.
pause

.venv\Scripts\python.exe run.py login --platform xiaohongshu

echo.
pause
