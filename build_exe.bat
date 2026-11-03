@echo off
setlocal
title Building LiteClip - don't click inside this window while it works
cd /d "%~dp0"

echo.
echo  TIP: clicking inside this window pauses it. If it ever looks stuck, press Enter.
echo.

rem ---- Python installed?
where python >nul 2>nul
if errorlevel 1 goto nopython

rem ---- Find the app file, even if the browser renamed it
set "SCRIPT="
if exist "liteclip.pyw" set "SCRIPT=liteclip.pyw"
if not defined SCRIPT for %%F in (liteclip*.pyw) do if not defined SCRIPT set "SCRIPT=%%F"
if not defined SCRIPT goto noscript

rem ---- Read the version number from the app
set "VER=1.0.0"
for /f "delims=" %%V in ('python -c "import re;print(re.search(r'^APP_VERSION = .(.+?).\s*$', open(r'%SCRIPT%', encoding='utf-8').read(), re.M).group(1))" 2^>nul') do set "VER=%%V"
echo  Building LiteClip %VER% from %SCRIPT%

echo "%CD%" | find /i "OneDrive" >nul
if not errorlevel 1 echo  Note: this folder is synced by OneDrive. If the build says a file is in use, pause OneDrive.

rem ---- Close any running LiteClip so its files can be replaced
taskkill /f /im LiteClip.exe >nul 2>nul
powershell -NoProfile -Command "Get-Process ffmpeg -ErrorAction SilentlyContinue | Where-Object { $_.Path -like '%CD%\dist\*' } | Stop-Process -Force" >nul 2>nul
rem Leftovers from the old single-file build
if exist "dist\LiteClip.exe" del /q "dist\LiteClip.exe" >nul 2>nul
if exist "dist\ffmpeg.exe" del /q "dist\ffmpeg.exe" >nul 2>nul

echo.
echo  [1/3] Installing what LiteClip needs...
python -m pip install --upgrade --disable-pip-version-check customtkinter pillow pystray pyinstaller
if errorlevel 1 goto fail

set "ICON="
if exist "liteclip.ico" set "ICON=--icon liteclip.ico"

echo.
echo  [2/3] Building the app - this can take a couple of minutes...
python -m PyInstaller --noconfirm --clean --onedir --windowed --name LiteClip ^
  %ICON% --collect-data customtkinter --hidden-import pystray._win32 "%SCRIPT%"
if errorlevel 1 goto fail

echo.
echo  [3/3] Building the setup wizard...
call :findiscc
if defined ISCC goto haveiscc
echo  The setup wizard is made with Inno Setup, a free tool that isn't installed yet.
choice /c YN /m "  Install Inno Setup now with winget"
if errorlevel 2 goto noinno
winget install --id JRSoftware.InnoSetup -e --accept-source-agreements --accept-package-agreements
call :findiscc
if not defined ISCC goto noinno
:haveiscc
"%ISCC%" /Qp /DMyAppVersion=%VER% liteclip.iss
if errorlevel 1 goto fail

echo.
echo  ===========================================================
echo   Done!  dist\LiteClip-Setup-%VER%.exe  is your installer.
echo   That one file is all people need to download.
echo  ===========================================================
start "" explorer dist
pause
exit /b 0

:findiscc
set "ISCC="
for %%P in ("%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe" "%ProgramFiles%\Inno Setup 6\ISCC.exe" "%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe") do if not defined ISCC if exist %%P set "ISCC=%%~P"
if not defined ISCC for /f "delims=" %%P in ('where iscc 2^>nul') do if not defined ISCC set "ISCC=%%P"
exit /b 0

:noinno
echo.
echo  Skipped the setup wizard. The app itself was still built in dist\LiteClip\
echo  Install Inno Setup from https://jrsoftware.org/isdl.php and run this again to make the installer.
start "" explorer dist
pause
exit /b 0

:nopython
echo  Python isn't installed, or wasn't added to PATH.
echo  Install it from https://www.python.org/downloads/ and tick
echo  "Add python.exe to PATH" on the first screen of the installer.
pause
exit /b 1

:noscript
echo.
echo  Can't find liteclip.pyw in this folder:
echo    %CD%
if exist "liteclip*.txt" echo  There is a .txt version here - rename it so it ends in .pyw instead.
echo.
echo  Put liteclip.pyw in this folder, next to build_exe.bat, then run this again.
start "" explorer .
pause
exit /b 1

:fail
echo.
echo  Something went wrong. Scroll up to see the error.
pause
exit /b 1
