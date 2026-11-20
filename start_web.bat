@echo off
rem IPSS-M 本地網頁介面 - Windows 啟動檔 (雙擊即可)
chcp 65001 >nul
cd /d "%~dp0"

set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (where python >nul 2>&1 && set "PY=python")
if not defined PY goto nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)" >nul 2>&1
if errorlevel 1 goto nopython

%PY% -c "import pandas, openpyxl" >nul 2>&1
if errorlevel 1 (
  echo 第一次執行，正在安裝必要的 Python 套件 ^(需要網路，只需一次^)...
  %PY% -m pip install -r requirements.txt
  if errorlevel 1 goto pipfail
)

%PY% ipssm_web.py %*
echo.
pause
exit /b 0

:nopython
echo 找不到 Python 3.8 以上版本。
echo 請先到 python.org 下載安裝 Python，安裝時務必勾選 "Add python.exe to PATH"，再重新雙擊這個檔案。
pause
exit /b 1

:pipfail
echo 套件安裝失敗，請確認網路連線後再試一次。
pause
exit /b 1
