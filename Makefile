.PHONY: build-gdelt clean-gdelt test dev dev-web dev-worker dev-inline

# ---------------------------------------------------------------------------
# Dev servers
# ---------------------------------------------------------------------------
# Two-process mode (recommended for iteration):
#   Terminal 1: `make dev-web`     — web only, --reload on every code change,
#                                    NEVER blocks on long pipeline runs.
#   Terminal 2: `make dev-worker`  — long-running worker, auto-restarts when
#                                    worker/pipeline/scoring code changes via
#                                    watchfiles. Refetches are picked up from
#                                    the brand_runs queue.
#
# Single-process fallback (legacy / quick demos):
#   `make dev-inline`              — uvicorn + inline jobs. UI edits stall
#                                    until any in-flight refetch finishes.
#                                    Don't use while iterating on pipeline code.
#
# `make dev` is an alias for the recommended two-process intent — prints
# the two commands to run instead of trying to background them itself.

VENV_BIN := .venv/bin
PORT ?= 8000
HOST ?= 127.0.0.1
# Dedicated local DB for signal-room. Created via:
#   createdb signal_room_local
# `override` forces this value even when the surrounding shell already exports
# DATABASE_URL (e.g. pointing at gbrain_local for another project). Override
# on the make command line if you actually want a different DB:
#   make dev-web DATABASE_URL=postgresql://.../somewhere_else
override DATABASE_URL := postgresql://danpeguine@localhost:5432/signal_room_local

dev:
	@echo "Open two terminals:"
	@echo "  Terminal 1:  make dev-web"
	@echo "  Terminal 2:  make dev-worker"
	@echo ""
	@echo "Or for a single-process demo:  make dev-inline"

dev-web:
	@echo "[dev-web] http://$(HOST):$(PORT) — INLINE_JOBS off, web reloads on edits"
	@echo "[dev-web] DATABASE_URL=$(DATABASE_URL)"
	SIGNAL_ROOM_INLINE_JOBS= \
	DATABASE_URL=$(DATABASE_URL) \
	$(VENV_BIN)/uvicorn signal_room.web:app \
		--host $(HOST) --port $(PORT) --reload \
		--reload-dir signal_room \
		--reload-exclude 'signal_room/worker.py' \
		--reload-exclude 'signal_room/pipeline.py' \
		--reload-exclude 'signal_room/llm_scoring.py' \
		--reload-exclude 'signal_room/planner.py' \
		--reload-exclude 'signal_room/fetchers/*'

dev-worker:
	@echo "[dev-worker] polling brand_runs queue · auto-restart on worker/pipeline edits"
	@echo "[dev-worker] DATABASE_URL=$(DATABASE_URL)"
	DATABASE_URL=$(DATABASE_URL) \
	$(VENV_BIN)/watchfiles --filter python \
		'$(VENV_BIN)/python -m signal_room.worker' \
		signal_room/worker.py \
		signal_room/pipeline.py \
		signal_room/llm_scoring.py \
		signal_room/planner.py \
		signal_room/fetchers \
		signal_room/scoring.py \
		signal_room/tracer.py

dev-worker-slim:
	@echo "[dev-worker-slim] SIGNAL_ROOM_SLIM_RUN=1 — caps LLM scoring to ~10 items × pillar"
	@echo "[dev-worker-slim] DATABASE_URL=$(DATABASE_URL)"
	SIGNAL_ROOM_SLIM_RUN=1 \
	DATABASE_URL=$(DATABASE_URL) \
	$(VENV_BIN)/watchfiles --filter python \
		'$(VENV_BIN)/python -m signal_room.worker' \
		signal_room/worker.py \
		signal_room/pipeline.py \
		signal_room/llm_scoring.py \
		signal_room/planner.py \
		signal_room/fetchers \
		signal_room/scoring.py \
		signal_room/tracer.py

dev-inline:
	@echo "[dev-inline] http://$(HOST):$(PORT) — inline jobs (single-process, UI stalls on long runs)"
	@echo "[dev-inline] DATABASE_URL=$(DATABASE_URL)"
	SIGNAL_ROOM_INLINE_JOBS=1 \
	DATABASE_URL=$(DATABASE_URL) \
	$(VENV_BIN)/uvicorn signal_room.web:app --host $(HOST) --port $(PORT) --reload



# Build the vendored gdelt-pp-cli binary into bin/. The signal-room fetcher
# discovers it automatically via its resolver chain.
build-gdelt:
	@mkdir -p bin
	cd vendor/gdelt-pp-cli-src && go build -o ../../bin/gdelt-pp-cli ./cmd/gdelt-pp-cli
	@echo "built: bin/gdelt-pp-cli"

clean-gdelt:
	rm -f bin/gdelt-pp-cli

test:
	python3 -m unittest discover tests
