@echo off
chcp 65001 >nul
cd /d "%~dp0"
py -3 -m venv .venv
if errorlevel 1 goto fail
.venv\Scripts\python.exe -m pip install -r requirements.txt
if errorlevel 1 goto fail
echo 설치 완료. 실행.bat 파일을 실행하세요.
pause
exit /b 0
:fail
echo 설치 실패. Python 3.11 이상과 인터넷 연결을 확인하세요.
pause
exit /b 1
