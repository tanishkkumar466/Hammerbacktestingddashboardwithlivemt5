@echo off
REM Build hammer_mt5_engine.exe (needs Visual Studio CMake / MSVC)
cd /d "%~dp0"
cmake -B build -G "Visual Studio 17 2022" -A x64
if errorlevel 1 (
  echo Trying default generator...
  cmake -B build -A x64
)
cmake --build build --config Release
echo.
echo Output: build\Release\hammer_mt5_engine.exe
echo Run:    build\Release\hammer_mt5_engine.exe
echo Health: http://127.0.0.1:17101/health
pause
