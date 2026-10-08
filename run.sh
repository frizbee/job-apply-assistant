#!/bin/bash
# Mac'te çalıştır: kalıcı Chrome profiliyle LinkedIn'e girer ve başvuruları yapar.
# İlk seferde açılan pencerede LinkedIn'e giriş yap; oturum .browser-profile/ içinde kalır.
cd "$(dirname "$0")"
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt; }
DRY_RUN="${DRY_RUN:-false}" .venv/bin/python apply.py
status=$?
git add data/ && { git diff --cached --quiet || git commit -qm "Apply run $(date +%F)"; }
exit $status
