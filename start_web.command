#!/bin/bash
# IPSS-M 本地網頁介面 - 啟動檔 (macOS: 雙擊 .command；Linux: 執行 ./start_web.sh)
cd "$(dirname "$0")" || exit 1

PY=""
for cand in python3 python; do
  if command -v "$cand" >/dev/null 2>&1 && "$cand" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 8) else 1)' 2>/dev/null; then
    PY="$cand"; break
  fi
done
if [ -z "$PY" ]; then
  echo "找不到 Python 3.8 以上版本。請先到 python.org 下載安裝 Python，再重新執行。"
  read -r -p "按 Enter 結束..." _
  exit 1
fi

if ! "$PY" -c 'import pandas, openpyxl' 2>/dev/null; then
  echo "第一次執行，正在安裝必要的 Python 套件 (需要網路，只需一次)..."
  "$PY" -m pip install -r requirements.txt || { echo "套件安裝失敗，請確認網路連線後再試一次。"; read -r -p "按 Enter 結束..." _; exit 1; }
fi

"$PY" ipssm_web.py "$@"
