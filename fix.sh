#!/usr/bin/env bash
# 立即检查并修复失效的 Facebook 图片；可传 --dry-run 只检查不发布。
set -euo pipefail
cd "$(dirname "$0")"

if [ -x .venv/bin/python3 ]; then
  PY=.venv/bin/python3
elif command -v python3 >/dev/null 2>&1; then
  PY=python3
else
  echo "[错误] 未找到 Python 3，请先安装。" >&2
  exit 1
fi

echo "开始检查 Facebook 图片；修复后将自动提交并推送，耗时取决于图片和商品数量。"
exec "$PY" -u check_fb_images.py "$@"
