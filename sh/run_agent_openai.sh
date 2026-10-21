#!/usr/bin/env bash
# 실제 openai 모델로 백엔드만 실행합니다. 기존 대화 DB는 보존합니다.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/run_service.py --backend openai --backend-only
