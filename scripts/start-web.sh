#!/usr/bin/env bash
set -Eeuo pipefail

# 统一从脚本所在项目根目录启动，避免 Linux 用户从任意工作目录运行时
# server.mjs 把任务路径解析到错误位置。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

command -v node >/dev/null 2>&1 || { echo "node 18+ is required" >&2; exit 2; }
if [[ -x "$ROOT/.venv/bin/python" ]]; then
  export SKILLSBENCH_PYTHON="$ROOT/.venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  export SKILLSBENCH_PYTHON="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  export SKILLSBENCH_PYTHON="$(command -v python)"
else
  echo "Python 3.12+ is required for trajectory analysis and repair" >&2
  exit 2
fi

exec node runner-app/server.mjs
