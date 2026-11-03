@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 (
  echo Python 3.11 이상을 python.org에서 설치해 주세요.
  pause
  exit /b 1
)
if not exist .venv\Scripts\python.exe py -3 -m venv .venv
if errorlevel 1 goto error
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto error
.venv\Scripts\python.exe app.py
if errorlevel 1 goto error
exit /b 0
:error
 echo 실행에 실패했습니다. 위 오류 메시지를 확인해 주세요.
 pause
 exit /b 1
