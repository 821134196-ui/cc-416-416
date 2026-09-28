#!/usr/bin/env bash
# 运行全部测试（纯标准库 unittest，无需安装任何依赖）
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m unittest discover -s tests -p "test_*.py" -v
