@echo off
setlocal
set "PORT=9876"

py -3.11 -c "import sys" >nul 2>nul
if %ERRORLEVEL% EQU 0 (
    set "PY_CMD=py -3.11"
) else (
    echo Python 3.11 was not found. Falling back to the default Python 3 runtime.
    echo For audio playback, install Python 3.11 and then run:
    echo   py -3.11 -m pip install --user pyo mido python-rtmidi
    echo.
    set "PY_CMD=py -3"
)

%PY_CMD% samplesplitter.py --dir mp3s --port %PORT%
echo.
echo Sample Splitter exited with code %ERRORLEVEL%.
pause
