@echo off
setlocal
pushd "%~dp0"
where py >nul 2>&1
if errorlevel 1 (
    python app.py %*
) else (
    py -3 app.py %*
)
if errorlevel 1 pause
popd
endlocal
