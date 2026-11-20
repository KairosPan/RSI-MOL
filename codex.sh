#!/bin/zsh
# Start Codex in the RSI trial environment: its own Codex home, and ./work as the working directory.
# Usage:  ./codex.sh            interactive Codex
#         ./codex.sh login      sign in (once)
#         ./codex.sh exec "..." one non-interactive turn
here="${0:A:h}"
export CODEX_HOME="$here/codex-home"
cd "$here/work" || exit 1
exec codex "$@"
