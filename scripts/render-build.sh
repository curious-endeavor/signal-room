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

# Install Go if missing so we can build the vendored gdelt-pp-cli.
# Render's default Python runtime doesn't include Go; we drop a private
# Go install into /tmp and add it to PATH for this build only. The
# resulting binary ships in bin/gdelt-pp-cli (no toolchain needed at runtime).
if ! command -v go >/dev/null 2>&1; then
  GO_VERSION="${GO_VERSION:-1.26.3}"
  GO_ARCH="linux-amd64"
  GO_TARBALL="go${GO_VERSION}.${GO_ARCH}.tar.gz"
  echo "installing Go ${GO_VERSION} into /tmp/go (for gdelt-pp-cli build)"
  curl -sSL "https://go.dev/dl/${GO_TARBALL}" -o "/tmp/${GO_TARBALL}"
  tar -C /tmp -xzf "/tmp/${GO_TARBALL}"
  export PATH="/tmp/go/bin:${PATH}"
  go version
fi

if command -v go >/dev/null 2>&1; then
  echo "go toolchain ready; building bin/gdelt-pp-cli"
  make build-gdelt
  ls -la bin/gdelt-pp-cli 2>&1 | head -1
else
  echo "no go toolchain — skipping bin/gdelt-pp-cli build (gdelt fetcher will gracefully skip at runtime)"
fi
