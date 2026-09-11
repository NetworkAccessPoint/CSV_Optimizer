#!/bin/bash
# csvopt 실행 파일을 만듭니다 (macOS / Linux). Python 3.9 이상이 필요합니다.
set -e
cd "$(dirname "$0")/.."
PY=${PYTHON:-python3}
echo "[1/2] PyInstaller 설치 중..."
"$PY" -m pip install --upgrade --quiet pyinstaller
echo "[2/2] 빌드 중... (몇 분 걸릴 수 있습니다)"
"$PY" -m PyInstaller --clean --noconfirm packaging/csvopt.spec
echo
echo "완료: dist/csvopt"
echo "  ./dist/csvopt ~/logs/app.csv 처럼 실행하세요."
