@echo off
setlocal
set "SHORTCUT=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\Clip Watcher.lnk"

if exist "%SHORTCUT%" (
    del "%SHORTCUT%"
    echo [OK] 시작프로그램 등록 해제됨
) else (
    echo [INFO] 등록된 항목이 없음
)

pause
endlocal
