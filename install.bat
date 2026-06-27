@echo off
setlocal

echo ========================================
echo   Mouse Control v0.3.0 - install deps
echo ========================================
echo.

set "PLUGIN_DIR=%~dp0"
set "RUNTIME_PYTHON=%PLUGIN_DIR%..\..\runtime\python.exe"
set "REQ_FILE=%PLUGIN_DIR%requirements.txt"

if not exist "%RUNTIME_PYTHON%" (
    echo [x] runtime\python.exe was not found.
    echo     Path: %RUNTIME_PYTHON%
    goto :end
)

if not exist "%REQ_FILE%" (
    echo [x] requirements.txt was not found.
    echo     Path: %REQ_FILE%
    goto :end
)

echo [i] Runtime Python: %RUNTIME_PYTHON%
echo [i] Requirements:   %REQ_FILE%
echo.
echo [i] Installing plugin dependencies...
"%RUNTIME_PYTHON%" -m pip install -r "%REQ_FILE%"
if errorlevel 1 (
    echo.
    echo [x] Dependency installation failed.
    echo     Try manually:
    echo     "%RUNTIME_PYTHON%" -m pip install -r "%REQ_FILE%"
    goto :end
)

echo.
echo [i] Verifying imports...
"%RUNTIME_PYTHON%" -c "import pyautogui; import PIL; import rapidocr_onnxruntime; print('dependencies ok')"
if errorlevel 1 (
    echo [x] Dependencies were installed, but import verification failed.
    goto :end
)

echo [ok] Mouse Control dependencies are ready.

:end
echo.
pause
