# Vendored source: gdelt-pp-cli

This directory is a snapshot of `~/printing-press/library/gdelt/` (the
Printing Press generator's output for the GDELT DOC 2.0 CLI). Kept in-tree
so deploy environments without access to Dan's local Printing Press install
(Render, CI, teammate checkouts) can build a working `gdelt-pp-cli` binary.

Module name: `gdelt-pp-cli` (see `go.mod`).
Required Go toolchain: ≥ 1.26.3 (see `go.mod`).

## Building

From the repo root:

```bash
make build-gdelt
```

Produces `bin/gdelt-pp-cli`. The signal-room fetcher's binary resolver
discovers it automatically — see `signal_room/fetchers/gdelt.py`.

## Refreshing

To pull a newer snapshot from the upstream generator:

```bash
rsync -av \
  --exclude='gdelt-pp-cli' --exclude='gdelt-pp-mcp' \
  --exclude='.printing-press*' --exclude='dist/' \
  ~/printing-press/library/gdelt/ \
  vendor/gdelt-pp-cli-src/
```

Then commit the diff and run `make build-gdelt` to verify the build still
succeeds.

## Upstream

- Generator: https://github.com/mvanhorn/cli-printing-press
- Library home: https://github.com/mvanhorn/cli-printing-press-library
- Snapshot taken: 2026-05-13 (this initial vendor)

The local generator install is not version-controlled, so an exact upstream
SHA is not pinned here. Track the upstream generator and library repos for
the canonical history.
