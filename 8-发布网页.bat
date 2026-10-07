@echo off
title 发布网页
cd /d "%~dp0"

echo ============================================================
echo   生成网页并发布到 GitHub
echo ============================================================
echo.
echo   1. 把库里的内容生成成静态网页（docs 目录）
echo   2. 内容有变化才提交并推送；没变化就什么都不做
echo.
echo   推送前会自动检查：data 目录里是登录态，绝不能被推到公开仓库，
echo   检查不通过会直接拦住不推。
echo.
echo   推完 1~2 分钟网页自动更新。
echo ============================================================
echo.
pause

.venv\Scripts\python.exe run.py publish --if-changed --push

echo.
pause
