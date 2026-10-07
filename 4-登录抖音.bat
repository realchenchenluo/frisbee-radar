@echo off
title 登录抖音
cd /d "D:\Desktop\飞盘讯息抓取"

echo ============================================================
echo   登录抖音（抓抖音才需要，只抓公众号可跳过）
echo ============================================================
echo.
echo   会打开浏览器窗口，请扫码登录。
echo   登录态保存到 data\browser_state\douyin，之后长期复用。
echo   不接触账号密码，只保存登录后的 cookie。
echo   登录成功后窗口自动关闭，最长等 3 分钟。
echo ============================================================
echo.
pause

.venv\Scripts\python.exe run.py login --platform douyin

echo.
pause
