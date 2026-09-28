#!/usr/bin/env bash
# 单条本地启动命令:重建样例数据(菜单/配餐/异常处置)并启动平台。
# 用法: ./run.sh        (默认 http://127.0.0.1:8000)
# 环境: PORT=8080 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

DB="${MEALCARE_DB:-data/mealcare.db}"
PORT="${PORT:-8000}"
HOST="${HOST:-127.0.0.1}"

# 每次启动重建,保证菜单、配餐、异常处置样例完整可复现
rm -f "$DB" "$DB-wal" "$DB-shm"

echo ">>> 生成演示数据并启动膳食核对平台 …"
exec python3 -m app --db "$DB" --port "$PORT" --host "$HOST"
