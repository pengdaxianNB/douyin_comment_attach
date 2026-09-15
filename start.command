#!/bin/zsh
cd "${0:A:h}"
if [[ ! -x .venv/bin/python ]]; then
  echo '请先按照 README.md 安装依赖。'
  exit 1
fi
exec .venv/bin/python main.py
