@echo off
REM Called by Windows Task Scheduler task. Do not double-click.
REM For manual runs use 1-生成日报.bat / 8-发布网页.bat instead.
REM
REM NOTE: keep this file ASCII-only in the log lines. The bat writes GBK
REM bytes, Python writes UTF-8, and mixing both in one log file garbles
REM whichever side loses. Timestamps and Chinese text come from Python.
cd /d "%~dp0"

REM rotate the log if it grows past ~2MB
for %%A in (data\daily.log) do if %%~zA GTR 2000000 move /y data\daily.log data\daily.log.old >nul 2>&1

echo. >> data\daily.log
echo -------------------- daily run start -------------------- >> data\daily.log
".venv\Scripts\python.exe" run.py daily --push >> data\daily.log 2>&1
echo -------------------- exit=%ERRORLEVEL% -------------------- >> data\daily.log
