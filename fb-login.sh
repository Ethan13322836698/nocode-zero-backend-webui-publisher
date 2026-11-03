#!/usr/bin/env bash
# 无头设备 Facebook 登录入口；使用与修复脚本相同的 Python 环境。
set -euo pipefail
cd "$(dirname "$0")"
if [ -x .venv/bin/python3 ]; then
  PY=.venv/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "[错误] 未找到 Python 3。" >&2
  exit 1
fi
exec "$PY" -u fb_login.py "$@"
