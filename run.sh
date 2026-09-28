#!/usr/bin/env bash
# 单条本地启动命令：
#   ./run.sh           生成菜单、配餐与异常处置样例（重建演示库）并启动平台
#   ./run.sh --keep    保留现有数据直接启动
#   PORT=8090 ./run.sh 自定义端口（默认 8026）
set -euo pipefail
cd "$(dirname "$0")"

export PYTHONUNBUFFERED=1
export MEAL_PORT="${PORT:-8026}"
export MEAL_DB="${MEAL_DB:-$(pwd)/data/mealcare.db}"

PY=python3

if [ "${1:-}" != "--keep" ]; then
  echo "▶ 生成匿名演示数据（菜单 / 批次配餐 / 标签 / 路线 / 异常处置样例）..."
  "$PY" app/seed.py
else
  echo "▶ 保留现有数据库：$MEAL_DB"
fi

echo "▶ 启动本地服务 ..."
exec "$PY" app/webapp.py
