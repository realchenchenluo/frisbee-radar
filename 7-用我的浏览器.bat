@echo off
chcp 65001 >nul
title 用我自己的浏览器
cd /d "D:\Desktop\飞盘讯息抓取"

echo ============================================================
echo   用调试端口启动你自己的浏览器（复用已有登录态）
echo ============================================================
echo.
echo   作用：让采集器直接复用你平时那个浏览器里**已经登录好的**会话，
echo         不用再扫码。跟 X 那套「启动带调试端口的Edge.bat」一个道理。
echo.
echo   注意：同一个浏览器 profile 不能开两个实例，
echo         所以需要先完全关掉现有的浏览器窗口。
echo         关掉后你的标签页会在下次启动时自动恢复。
echo ============================================================
echo.

set PORT=9222
set CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe
set EDGE=C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe

if exist "%CHROME%" (
  set BROWSER="%CHROME%"
  set PROFILE=%LOCALAPPDATA%\Google\Chrome\User Data
  set PROCNAME=chrome.exe
) else if exist "%EDGE%" (
  set BROWSER="%EDGE%"
  set PROFILE=%LOCALAPPDATA%\Microsoft\Edge\User Data
  set PROCNAME=msedge.exe
) else (
  echo [X] 找不到 Chrome 也找不到 Edge。
  echo     请改本文件里的 CHROME 变量指向你的浏览器。
  pause
  exit /b 1
)

tasklist /FI "IMAGENAME eq %PROCNAME%" 2>nul | find /I "%PROCNAME%" >nul
if %ERRORLEVEL%==0 (
  echo   检测到 %PROCNAME% 正在运行。要复用登录态必须先关掉它。
  echo.
  choice /C YN /M "  现在关闭它吗"
  if errorlevel 2 (
    echo   已取消。请手动关掉浏览器后再双击本文件。
    pause
    exit /b 1
  )
  taskkill /IM %PROCNAME% >nul 2>&1
  timeout /t 4 /nobreak >nul
  taskkill /IM %PROCNAME% /F >nul 2>&1
  timeout /t 2 /nobreak >nul
)

echo.
echo   正在启动带调试端口 %PORT% 的浏览器...
start "" %BROWSER% --remote-debugging-port=%PORT% --user-data-dir="%PROFILE%" ^
  https://www.xiaohongshu.com/explore https://www.douyin.com/

echo   等待浏览器就绪...
timeout /t 10 /nobreak >nul

echo.
echo   测试调试端口...
curl -s http://127.0.0.1:%PORT%/json/version >nul 2>&1
if %ERRORLEVEL%==0 (
  echo   [OK] 调试端口已就绪。
  echo.
  echo   ------------------------------------------------------------
  echo   接下来在刚启动的浏览器里正常登录一遍小红书/抖音（只需一次），
  echo   然后可以直接双击「1-生成日报.bat」采集，不用再扫码。
  echo.
  echo   如果采集时提示未登录，先单独采集一次确认：
  echo     .venv\Scripts\python.exe run.py crawl --source xiaohongshu
  echo   ------------------------------------------------------------
  echo.
  echo   提示：采集期间请保持这个浏览器开着，别关。
) else (
  echo   [X] 调试端口没响应。
  echo     可能是浏览器版本禁用了远程调试，或启动失败。
  echo     也可以试试把本文件里的 CHROME 换成 Edge 的路径。
)

echo.
pause
