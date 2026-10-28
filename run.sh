#!/usr/bin/env sh
# Rate every demo in the demos folder.
cd "$(dirname "$0")" || exit 1
if [ ! -x .venv/bin/python ]; then
    echo "First run: setting up Python environment..."
    python3 -m venv .venv && .venv/bin/python -m pip install -r requirements.txt || exit 1
fi
.venv/bin/python anchor-stats.py "$@"
