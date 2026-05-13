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

# Build the vendored GDELT CLI when a Go toolchain is available. Render's
# default Python runtime does not include Go; if/when that becomes the
# bottleneck, either:
#   - switch render.yaml to a build image that includes Go ≥ 1.26.3, or
#   - flip this project to shipping prebuilt binaries (vendor option a).
# Until then, this block is a no-op on production builds and the gdelt
# fetcher will surface a clear "no binary" error at runtime if invoked.
if command -v go >/dev/null 2>&1; then
  echo "go toolchain detected; building bin/gdelt-pp-cli"
  make build-gdelt
else
  echo "no go toolchain — skipping bin/gdelt-pp-cli build (gdelt fetcher will be inactive)"
fi
