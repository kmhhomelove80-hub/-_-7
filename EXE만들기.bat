@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist .venv\Scripts\python.exe goto fail
call .venv\Scripts\activate.bat
python -m pip install -r requirements-build.txt
if errorlevel 1 goto fail
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\build_windows.ps1
if errorlevel 1 goto fail
echo dist\KiwoomTwoStockTrader.exe 파일을 사용하세요.
pause
exit /b 0
:fail
echo 빌드 실패. 설치.bat 실행 여부와 화면 오류를 확인하세요.
pause
exit /b 1
