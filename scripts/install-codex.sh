#!/bin/sh
set -eu
install_root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
codex_bin=${CODEX_BIN:-codex}
[ "$(uname -s)" = Darwin ] || { echo 'This plugin requires macOS.' >&2; exit 1; }
command -v "$codex_bin" >/dev/null 2>&1 || { echo 'Set CODEX_BIN to your Codex CLI executable, or add codex to PATH.' >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3, 9), "Python 3.9+ required"'
xcrun --find clang >/dev/null
sh "$install_root/plugins/reference-native-test/build-picker.sh"
"$codex_bin" plugin marketplace add "$install_root" --json
"$codex_bin" plugin add reference-panel@reference-panel --json
echo 'Installed. Open Reference Panel in the Codex chat sidebar. If an older backend is still running, reopen Codex.'
