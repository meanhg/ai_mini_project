#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ ! -x .venv/bin/python ]; then
    echo "먼저 bash sh/setup_venv.sh 로 실행 환경을 준비해주세요."
    exit 1
fi
exec .venv/bin/python scripts/run_service.py "$@"
