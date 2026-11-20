@echo off
setlocal
set "SILENT=0"
if /i "%~1"=="--silent" set "SILENT=1"
set "HERE=%~dp0"
echo Installing Manju Publish Assistant (about 1-3 minutes). Please keep this window open.
set "PY=%HERE%runtime\python.exe"
if not exist "%PY%" (
  echo Runtime python missing. Please extract the package again.
  if "%SILENT%"=="0" pause
  exit /b 5
)
rem --- restore VC runtime DLLs before runtime\python.exe starts (they ship as *.dll.__rt) ---
set "VCDLLS=vcruntime140.dll vcruntime140_1.dll msvcp140.dll msvcp140_1.dll msvcp140_2.dll concrt140.dll"
for %%D in ("%HERE%runtime") do if exist "%%~D" for %%F in (%VCDLLS%) do if exist "%%~D\%%F.__rt" if not exist "%%~D\%%F" copy /y "%%~D\%%F.__rt" "%%~D\%%F" >nul 2>&1

"%PY%" "%HERE%scripts\bootstrap_py.py" --program "%HERE%."
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo Installation incomplete. Error code: %RC%.
  echo The reason is in install-log.txt on your Desktop - send that file to support.
  if "%SILENT%"=="0" pause
  exit /b %RC%
)
echo.
echo Done. Double-click the assistant icon on your Desktop to start.
if "%SILENT%"=="0" pause
exit /b 0
