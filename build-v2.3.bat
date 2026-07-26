@echo off
REM Build V2.3 separately. Existing V1.1, V2, V2.1, and V2.2 outputs are never overwritten.
setlocal
cd /d "%~dp0"

echo.
echo [1/5] Checking Python...
python --version >nul 2>&1
if errorlevel 1 (
  echo Python was not found. Install Python 3.10 or newer and add it to PATH.
  pause
  exit /b 1
)

echo [2/5] Checking the bundled scrcpy runtime...
if not exist "scrcpy-runtime\scrcpy.exe" goto :runtime_missing
if not exist "scrcpy-runtime\scrcpy-server" goto :runtime_missing
if not exist "scrcpy-runtime\adb.exe" goto :runtime_missing

echo [3/5] Installing Qt and packaging dependencies...
python -m pip install -r requirements.txt
if errorlevel 1 (
  echo Dependency installation failed.
  pause
  exit /b 1
)

echo [4/5] Running the automated test suite...
python -m unittest discover -s tests -p "test_*.py"
if errorlevel 1 (
  echo Tests failed. The V2.3 executable was not built.
  pause
  exit /b 1
)

echo [5/5] Building Convrse Device Control 2.3...
python -m PyInstaller --noconfirm --clean --distpath versions\windows\v2.3 --workpath build-v2.3 scrcpy-remote-v2.3.spec
if errorlevel 1 (
  echo Build failed. Review the output above.
  pause
  exit /b 1
)

echo.
if exist "versions\windows\v2.3\Convrse-Device-Control-V2.3.exe" (
  echo DONE -^> versions\windows\v2.3\Convrse-Device-Control-V2.3.exe
) else (
  echo Build finished without the expected executable. Review the output above.
  pause
  exit /b 1
)
echo.
pause
exit /b 0

:runtime_missing
echo.
echo The V2.3 runtime is incomplete.
echo Place the complete Windows scrcpy distribution, including scrcpy.exe,
echo scrcpy-server, adb.exe, and every supplied DLL, in:
echo   %CD%\scrcpy-runtime
echo.
echo No existing V2.2 files or builds were changed.
pause
exit /b 1
