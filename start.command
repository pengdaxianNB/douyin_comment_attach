#!/bin/zsh
cd "${0:A:h}"
if ! command -v uv >/dev/null 2>&1; then
  for p in "$HOME/.local/bin/uv" "$HOME/.cargo/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv"; do
    [[ -x "$p" ]] && export PATH="${p:h}:$PATH" && break
  done
fi
if ! command -v uv >/dev/null 2>&1; then
  echo '未找到 uv，请先安装：curl -LsSf https://astral.sh/uv/install.sh | sh'
  echo '安装后重新打开终端再试。'
  read -k 1 "?按任意键退出..."
  exit 1
fi
exec uv run main.py
