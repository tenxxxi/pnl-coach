#!/usr/bin/env bash
# PnL Coach 실행 스크립트 — venv 없으면 만들고 의존성 설치 후 기동
set -e
cd "$(dirname "$0")"

if [ ! -d venv ]; then
  python3 -m venv venv
  ./venv/bin/pip install -q -r requirements.txt
fi

exec ./venv/bin/uvicorn app:app --host 0.0.0.0 --port "${PORT:-8777}"
