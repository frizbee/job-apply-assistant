#!/bin/bash
# Kaynak koddan çalıştırma (Mac/Linux): arayüzü tarayıcıda açar.
# Terminal modu için: ./run.sh --terminal
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || python3 -m venv .venv
.venv/bin/pip install -q --disable-pip-version-check -r requirements.txt
if [ "$1" = "--terminal" ]; then
  DRY_RUN="${DRY_RUN:-false}" exec .venv/bin/python apply.py
fi
exec .venv/bin/python gui.py
