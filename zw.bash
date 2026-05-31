#!/bin/bash

# Path of the tool root dir
SCRIPT_PATH="$(readlink -f "$0")"
ZW_DIR="$(dirname "$SCRIPT_PATH")"

WORK_DIR="$(pwd)"

# Start the tool with uv env of the tool directory
exec uv --directory "$ZW_DIR" run streamlit run zenodo_viewer.py
