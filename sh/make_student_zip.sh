#!/usr/bin/env bash
# 대화·체크포인트·측정·벡터 DB와 실제 .env를 제외한 배포 ZIP을 만든다.
set -euo pipefail
cd "$(dirname "$0")/.."
exec .venv/bin/python scripts/package_project.py "$@"
