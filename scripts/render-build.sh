#!/usr/bin/env bash
set -euo pipefail

LAST30DAYS_REF="1f7e85a03f262345e992ddebd7b0c121c2f2862e"
LAST30DAYS_DIR="vendor/last30days-skill"

if [ ! -d "$LAST30DAYS_DIR/scripts" ]; then
  mkdir -p vendor
  git clone https://github.com/mvanhorn/last30days-skill.git "$LAST30DAYS_DIR"
  git -C "$LAST30DAYS_DIR" checkout "$LAST30DAYS_REF"
fi

python -m pip install .
