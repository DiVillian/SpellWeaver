@echo off
rem Start SpellWeaver on Windows by double-clicking this file.
rem
rem Everything it needs is Python 3. It finds it, starts the server in this
rem window, and opens the page. Close the window, or use Shut Down on the
rem Settings page, to stop it.
setlocal
title SpellWeaver

rem CMD cannot use a network path as its working directory. Left alone it says
rem so and carries on from C:\Windows, where none of this exists, so the error
rem that follows is about a missing run.py rather than about the real problem.
set "HERE=%~dp0"
if "%HERE:~0,2%"=="\\" (
  echo.
  echo  SpellWeaver cannot run from a network path:
  echo.
  echo    %HERE%
  echo.
  echo  Windows will not let a command window work there. Copy the folder to a
  echo  local drive, for example C:\SpellWeaver, and run it from the copy.
  echo.
  pause
  exit /b 1
)

cd /d "%HERE%"

if not exist "run.py" (
  echo.
  echo  run.py is not in this folder, so this is not a SpellWeaver folder:
  echo.
  echo    %HERE%
  echo.
  echo  Keep this file alongside run.py rather than moving it somewhere else.
  echo.
  pause
  exit /b 1
)

rem "py" is the Windows Python launcher and the one to prefer: plain "python"
rem is often a Microsoft Store stub that opens the Store instead of running.
rem Each candidate has to actually execute, not merely be on the PATH.
set "PY="
py -3 -c "import sys" >nul 2>nul && set "PY=py -3"
if not defined PY (
  python -c "import sys" >nul 2>nul && set "PY=python"
)
if not defined PY (
  python3 -c "import sys" >nul 2>nul && set "PY=python3"
)

if not defined PY (
  echo.
  echo  Python 3 was not found.
  echo.
  echo  Install it from https://www.python.org/downloads/ and tick
  echo  "Add python.exe to PATH" in the installer, then run this again.
  echo.
  pause
  exit /b 1
)

echo.
echo  Starting SpellWeaver. This window is its console: closing it stops the
echo  server. The page opens by itself in a moment.
echo.

%PY% run.py --open %*
set "CODE=%ERRORLEVEL%"

rem A clean stop needs no ceremony: the window closes. It only waits when
rem something went wrong, because that is the only time there is anything left
rem to read, and a window that vanishes takes the reason with it.
if "%CODE%"=="0" exit /b 0

echo.
echo  SpellWeaver stopped with an error. The reason is above.
echo  A common one is port 8800 already being in use, which happens when it is
echo  already running in another window.
echo.
pause
exit /b %CODE%
