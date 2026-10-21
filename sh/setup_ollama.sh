#!/usr/bin/env bash
# .env의 채팅·임베딩 모델 준비. OLLAMA_FORCE_REFRESH=1은 해당 모델만 다시 받는다.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/setup_ollama.py "$@"
