@echo off
chcp 65001 >nul
setlocal enabledelayedexpansion

echo ========================================
echo   Mouse Control v0.2.0 - 安装依赖
echo ========================================
echo.

REM ── 定位 runtime python ─────────────────
set "RUNTIME_PYTHON=%~dp0..\..\runtime\python.exe"
if not exist "%RUNTIME_PYTHON%" (
    echo [×] 找不到 runtime\python.exe
    echo     路径: %RUNTIME_PYTHON%
    echo     请确认项目根目录下的 runtime 文件夹存在。
    goto :end
)

echo [i] Runtime Python: %RUNTIME_PYTHON%
echo.

REM ── 检查是否已安装 ───────────────────────
echo [i] 检查 pyautogui 是否已安装 ...
"%RUNTIME_PYTHON%" -c "import pyautogui; print('pyautogui', pyautogui.__version__)" 2>nul
if %errorlevel% equ 0 (
    echo [√] pyautogui 已安装，无需重复安装。
    goto :end
)

REM ── 安装 ─────────────────────────────────
echo [i] 正在安装 pyautogui ...
echo.
"%RUNTIME_PYTHON%" -m pip install pyautogui --quiet
echo.

if %errorlevel% equ 0 (
    echo [√] pyautogui 安装成功！
) else (
    echo [×] 安装失败。可能原因：
    echo     1. 网络连接问题
    echo     2. pip 版本过旧
    echo     3. 需要管理员权限
    echo.
    echo     请尝试手动运行:
    echo     "%RUNTIME_PYTHON%" -m pip install pyautogui
)

:end
echo.
pause
