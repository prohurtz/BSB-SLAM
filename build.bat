@echo off
setlocal

REM ============================================
REM   Beyond-SLAM build script
REM   Usage:
REM     build.bat            - build x64 Release
REM     build.bat debug      - build x64 Debug
REM ============================================

set "CONFIG=Release"
if /i "%~1"=="debug" set "CONFIG=Debug"

REM === Locate MSBuild via vswhere ===
set "VSWHERE=%ProgramFiles(x86)%\Microsoft Visual Studio\Installer\vswhere.exe"
if not exist "%VSWHERE%" set "VSWHERE=%ProgramFiles%\Microsoft Visual Studio\Installer\vswhere.exe"

set "MSBUILD="
if exist "%VSWHERE%" (
    for /f "usebackq tokens=*" %%i in (`"%VSWHERE%" -latest -requires Microsoft.Component.MSBuild -find MSBuild\**\Bin\MSBuild.exe`) do set "MSBUILD=%%i"
)

REM Fallback: msbuild already on PATH (e.g. Developer Command Prompt)
if not defined MSBUILD (
    where msbuild >nul 2>nul
    if not errorlevel 1 set "MSBUILD=msbuild"
)

if not defined MSBUILD (
    echo ERROR: MSBuild not found. Install Visual Studio 2022 with C++ workload,
    echo or run this from a Developer Command Prompt.
    exit /b 1
)

echo Using MSBuild: %MSBUILD%
echo Building %CONFIG% x64...
echo.

"%MSBUILD%" "%~dp0Driver\Beyond-SLAM.sln" /p:Configuration=%CONFIG% /p:Platform=x64 /m /v:minimal
if errorlevel 1 (
    echo.
    echo BUILD FAILED.
    exit /b 1
)

echo.
echo Build succeeded: Driver\x64\%CONFIG%\driver_BSB-SLAM.dll

exit /b 0
