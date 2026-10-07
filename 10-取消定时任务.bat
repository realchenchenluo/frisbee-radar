@echo off
title 取消定时任务
cd /d "%~dp0"

set TASK=飞盘讯息每日更新

echo ============================================================
echo   取消每天 21:00 的自动更新
echo ============================================================
echo.
schtasks /query /tn "%TASK%" >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
  echo   没有找到任务「%TASK%」，可能已经取消了。
  echo.
  pause
  exit /b 0
)

choice /C YN /M "  确定要取消吗"
if errorlevel 2 (
  echo   已保留，没有改动。
  pause
  exit /b 1
)

schtasks /delete /tn "%TASK%" /f
if %ERRORLEVEL%==0 (
  echo.
  echo   [OK] 已取消。手动双击入口仍然可以用。
) else (
  echo   [X] 取消失败，可以在「任务计划程序」里手动删。
)
echo.
pause
