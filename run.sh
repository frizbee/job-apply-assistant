#!/bin/bash
# Mac'te çalıştır: li_at çerezini Anahtar Zinciri'nden okur ve başvuruları yapar.
cd "$(dirname "$0")"
LI_AT=$(security find-generic-password -a "$USER" -s linkedin-li_at -w 2>/dev/null)
if [ -z "$LI_AT" ]; then
  echo "li_at çerezi Anahtar Zinciri'nde yok. Kaydetmek için:"
  echo '  read -s "?li_at: " v && security add-generic-password -a "$USER" -s linkedin-li_at -w "$v" -U'
  exit 1
fi
[ -x .venv/bin/python ] || { python3 -m venv .venv && .venv/bin/pip install -q -r requirements.txt && .venv/bin/playwright install chromium; }
LI_AT="$LI_AT" DRY_RUN="${DRY_RUN:-false}" .venv/bin/python apply.py
status=$?
git add data/ && { git diff --cached --quiet || git commit -qm "Apply run $(date +%F)"; }
exit $status
