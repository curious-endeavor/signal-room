#!/usr/bin/env bash
set -euo pipefail

LAST30DAYS_REF="1f7e85a03f262345e992ddebd7b0c121c2f2862e"
LAST30DAYS_DIR="vendor/last30days-skill"

if [ ! -d "$LAST30DAYS_DIR/scripts" ]; then
  mkdir -p vendor
  git clone https://github.com/mvanhorn/last30days-skill.git "$LAST30DAYS_DIR"
  git -C "$LAST30DAYS_DIR" checkout "$LAST30DAYS_REF"
fi

python -c 'from pathlib import Path; p=Path("vendor/last30days-skill/scripts/lib/env.py"); text=p.read_text(); needle="        ('\''APIFY_API_TOKEN'\'', None),\n"; replacement=needle+"        ('\''GITHUB_TOKEN'\'', None),\n"; p.write_text(text if "('\''GITHUB_TOKEN'\''" in text else text.replace(needle, replacement))'

python -m pip install .
