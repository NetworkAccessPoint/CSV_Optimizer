@echo off
chcp 65001 >nul 2>&1
rem csvopt.exe 를 만듭니다. Python 3.9 이상과 인터넷 연결이 필요합니다.
setlocal
cd /d "%~dp0.."
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [csvopt] Python 3.9 이상이 필요합니다. https://www.python.org/downloads/
  pause
  exit /b 1
)
echo [1/2] PyInstaller 설치 중...
%PY% -m pip install --upgrade --quiet pyinstaller || goto :fail
echo [2/2] 빌드 중... (몇 분 걸릴 수 있습니다)
%PY% -m PyInstaller --clean --noconfirm packaging\csvopt.spec || goto :fail
echo.
echo 완료: dist\csvopt.exe
echo   csvopt.exe 를 더블클릭하거나, CSV 파일을 그 위로 끌어다 놓으세요.
pause
exit /b 0
:fail
echo 빌드에 실패했습니다.
pause
exit /b 1
