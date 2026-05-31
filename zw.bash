#!/bin/bash

# Alternative launcher (no uv tool install needed)
# Usage: bash zw.bash
#        or: ln -s /path/to/zw.bash ~/.local/bin/zw

SCRIPT_PATH="$(readlink -f "$0")"
ZW_DIR="$(dirname "$SCRIPT_PATH")"

exec uv --directory "$ZW_DIR" run streamlit run zenodo_viewer.py
