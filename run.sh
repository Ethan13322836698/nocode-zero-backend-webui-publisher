#!/usr/bin/env bash
# 黑白极简 · 商品店铺 · 一键启动
# 用法: bash run.sh
set -e

cd "$(dirname "$0")"

PY=""
for cmd in python3 python; do
  if command -v "$cmd" >/dev/null 2>&1; then
    PY="$cmd"
    break
  fi
done

if [ -z "$PY" ]; then
  echo "[错误] 未找到 python3 或 python，请先安装 Python 3。"
  exit 1
fi

echo "启动本地管理后台…"
exec "$PY" server.py
