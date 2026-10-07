@echo off
title 设置每天 21:00 自动更新
cd /d "%~dp0"

set TASK=飞盘讯息每日更新
set RUNNER=%~dp0auto_daily.bat

echo ============================================================
echo   设置每天 21:00 自动更新日报和网页
echo ============================================================
echo.
echo   会创建一个 Windows 计划任务：「%TASK%」
echo.
echo   每天 21:00 它做这些事：
echo     1. 采集各平台新内容
echo     2. 有新内容才出日报；没有就什么都不做
echo     3. 网页内容有变化才更新并推送
echo.
echo   原则：没有新内容 = 什么都不做。
echo   不会为了「看起来有更新」而改动任何东西，也不会编造内容。
echo.
echo   运行日志：data\daily.log
echo   想取消：双击「10-取消定时任务.bat」
echo ============================================================
echo.
pause

schtasks /query /tn "%TASK%" >nul 2>&1
if %ERRORLEVEL%==0 (
  echo   已存在同名任务，先删除旧的...
  schtasks /delete /tn "%TASK%" /f >nul
)

schtasks /create /tn "%TASK%" /tr "%RUNNER%" /sc daily /st 21:00 /f

if %ERRORLEVEL%==0 (
  echo.
  echo   [OK] 已设置，任务计划程序里可以看到「%TASK%」。
) else (
  echo.
  echo   [X] 创建失败。可以手动在「任务计划程序」里建：
  echo       操作：启动程序 - %RUNNER%
  echo       触发器：每天 21:00
)

echo.
pause
