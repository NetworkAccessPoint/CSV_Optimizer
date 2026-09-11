#!/bin/bash
# csvopt 실행 스크립트 (macOS). Finder 에서 더블클릭하면 브라우저로 열립니다.
cd "$(dirname "$0")" || exit 1
if command -v python3 >/dev/null 2>&1; then
  exec python3 -m csvopt "$@"
fi
echo "[csvopt] python3 를 찾을 수 없습니다."
echo "         터미널에서 'xcode-select --install' 또는 https://www.python.org/downloads/ 로 설치하세요."
read -r -p "엔터를 누르면 닫힙니다..." _
exit 1
