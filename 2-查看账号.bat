@echo off
title 查看已采到的账号
cd /d "D:\Desktop\飞盘讯息抓取"

echo ============================================================
echo   已采到的账号列表
echo ============================================================
echo.
echo   用途：config\sources.yaml 里的 watch.authoritative（权威号），
echo   填的号名必须和下面列出的完全一致，否则匹配不上。
echo   标了「权威号」的就是当前配置里已生效的。
echo ============================================================
echo.

.venv\Scripts\python.exe run.py authors --limit 80

echo.
pause
