@echo off
REM Creates a shortcut in the user's Startup folder so clip-watcher launches at login.
setlocal

set "SCRIPT_DIR=%~dp0"
set "TARGET=%SCRIPT_DIR%clip-watcher.bat"
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "SHORTCUT=%STARTUP%\Clip Watcher.lnk"

powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "$ws = New-Object -ComObject WScript.Shell;" ^
  "$sc = $ws.CreateShortcut('%SHORTCUT%');" ^
  "$sc.TargetPath = '%TARGET%';" ^
  "$sc.WorkingDirectory = '%SCRIPT_DIR%';" ^
  "$sc.WindowStyle = 7;" ^
  "$sc.Save()"

if exist "%SHORTCUT%" (
    echo.
    echo [OK] 시작프로그램 등록 완료
    echo      %SHORTCUT%
    echo.
    echo 해제하려면 위 .lnk 파일을 지우거나 uninstall-startup.bat 실행
) else (
    echo [ERROR] 등록 실패
)

pause
endlocal
