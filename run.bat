@echo off
chcp 65001 >nul 2>&1
rem CSV Insight launcher for Windows.
rem CSV 파일을 이 배치 파일 위로 끌어다 놓으면 그 파일이 바로 열립니다.
setlocal
cd /d "%~dp0"
set "PY="
where py >nul 2>&1 && set "PY=py -3"
if not defined PY (
  where python >nul 2>&1 && set "PY=python"
)
if not defined PY (
  echo [CSV Insight] Python 3.9 이상이 필요합니다.
  echo          https://www.python.org/downloads/ 에서 설치한 뒤 다시 실행하세요.
  echo          설치할 때 "Add python.exe to PATH" 를 반드시 체크하세요.
  pause
  exit /b 1
)
%PY% -m csvinsight %*
if errorlevel 1 pause
