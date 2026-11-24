@echo off
setlocal
pushd "%~dp0" || exit /b 1

set "python_cmd=python"
set "python_check=import sys, tkinter; sys.exit(sys.version_info < (3, 10))"
python -B -c "%python_check%" >nul 2>&1
if errorlevel 1 (
    set "python_cmd=py -3"
    py -3 -B -c "%python_check%" >nul 2>&1
    if errorlevel 1 (
        echo PixNova Test Station requires Python 3.10 or later with Tcl/Tk.
        pause
        popd
        exit /b 1
    )
)

%python_cmd% app.py %*
set "app_status=%ERRORLEVEL%"
if not "%app_status%"=="0" pause
popd
exit /b %app_status%
