#!/bin/zsh
# Double-clickable macOS launcher. If double-click does nothing, run:
#   chmod +x start.command
cd "$(dirname "$0")" || exit 1
python3 -m flashdrop
status=$?
if [ "$status" -ne 0 ]; then
  echo
  read -r -n 1 -s -p "启动失败。按任意键关闭窗口…"
  echo
fi
