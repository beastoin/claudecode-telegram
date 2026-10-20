# claudecode-telegram Makefile
#
# Deterministic test commands — no manual env vars needed.
# Token loaded from ~/.config/claudecode-telegram/test.env
# TEST_CHAT_ID is NEVER set (defaults to fake 123456789 in test.sh).

SHELL := /bin/bash
TEST_ENV := $(HOME)/.config/claudecode-telegram/test.env
TRIASSIC4_HOST := triassic-4
TRIASSIC4_USER := bridge-test
TRIASSIC4_PYTHON := python3.12

# Load token from test.env (extract TELEGRAM_BOT_TOKEN only, never ADMIN_CHAT_ID)
TOKEN = $(shell grep '^TELEGRAM_BOT_TOKEN=' $(TEST_ENV) 2>/dev/null | cut -d= -f2)

.PHONY: test test-fast test-full test-remote test-pytest lint check cd help

help: ## Show available targets
	@grep -E '^[a-z].*:.*## ' $(MAKEFILE_LIST) | sort | awk -F ':.*## ' '{printf "  %-20s %s\n", $$1, $$2}'

test: ## Run default mode tests (unit + integration, ~2-3 min)
	@test -n "$(TOKEN)" || { echo "ERROR: No token in $(TEST_ENV)"; exit 1; }
	TEST_BOT_TOKEN="$(TOKEN)" ./test.sh

test-fast: ## Run FAST mode tests (unit + CLI only, ~15s)
	@test -n "$(TOKEN)" || { echo "ERROR: No token in $(TEST_ENV)"; exit 1; }
	FAST=1 TEST_BOT_TOKEN="$(TOKEN)" ./test.sh

test-full: ## Run FULL mode tests (unit + integration + tunnel, ~5 min)
	@test -n "$(TOKEN)" || { echo "ERROR: No token in $(TEST_ENV)"; exit 1; }
	FULL=1 TEST_BOT_TOKEN="$(TOKEN)" ./test.sh

test-pytest: ## Run pytest only (no bash e2e)
	python3 -m pytest tests/ -q --tb=short

test-remote: ## Run FAST tests on triassic-4 via beast host (cross-platform validation)
	@test -n "$(TOKEN)" || { echo "ERROR: No token in $(TEST_ENV)"; exit 1; }
	rsync -az --exclude='.git' --exclude='__pycache__' --exclude='node_modules' \
		--exclude='.pytest_cache' --exclude='.venv' \
		. $(TRIASSIC4_HOST):/data/beast/home-store/$(TRIASSIC4_USER)/claudecode-telegram/
	ssh $(TRIASSIC4_HOST) "chown -R $(TRIASSIC4_USER):agents /data/beast/home-store/$(TRIASSIC4_USER)/claudecode-telegram/ && \
		chmod +x /data/beast/home-store/$(TRIASSIC4_USER)/claudecode-telegram/test.sh \
		         /data/beast/home-store/$(TRIASSIC4_USER)/claudecode-telegram/bridge.sh \
		         /data/beast/home-store/$(TRIASSIC4_USER)/claudecode-telegram/claudecode.sh"
	ssh $(TRIASSIC4_HOST) "beast host exec $(TRIASSIC4_USER) -- bash -c '\
		export PATH=/opt/beast/shared/bin:\$$PATH && \
		cd ~/claudecode-telegram && \
		PYTHON=$(TRIASSIC4_PYTHON) FAST=1 TEST_BOT_TOKEN=\"$(TOKEN)\" ./test.sh'"

test-remote-pytest: ## Run pytest only on triassic-4
	ssh $(TRIASSIC4_HOST) "beast host exec $(TRIASSIC4_USER) -- bash -c '\
		cd ~/claudecode-telegram && \
		/opt/beast/shared/bin/$(TRIASSIC4_PYTHON) -m pytest tests/ -q --tb=short'"

check: ## Pre-commit lint: Python lint + test.sh hygiene (run before every commit)
	@echo "── Python lint (ruff) ──"
	@ruff check bridge.py claudecode.py telegram.py shared.py || { echo "❌ ruff failed"; exit 1; }
	@echo "── Python format (black --check) ──"
	@black --check --line-length 120 bridge.py claudecode.py telegram.py shared.py 2>&1 || { echo "❌ black failed"; exit 1; }
	@echo "── test.sh hygiene ──"
	@# Catch hardcoded python3 -c commands (must use "$$PYTHON" -c for cross-platform)
	@# Only match actual command invocations, not string literals in test assertions
	@if grep -En '^\s*python3 -c|[`$$\(]python3 -c' test.sh | grep -v '^\s*#' | head -5 | grep -q .; then \
		echo "❌ test.sh: found hardcoded 'python3 -c' — use \"\$$PYTHON\" -c"; \
		grep -En '^\s*python3 -c|[`$$\(]python3 -c' test.sh | grep -v '^\s*#' | head -3; exit 1; fi
	@# Catch deleted module imports
	@if grep -q 'from connectors import' test.sh 2>/dev/null; then \
		echo "❌ test.sh: 'from connectors import' — connectors.py was deleted, use 'from bridge import'"; \
		grep -n 'from connectors import' test.sh | head -3; exit 1; fi
	@# Catch real chat ID in test commands (check test.sh only — Makefile self-references are fine)
	@if grep -qE 'TEST_CHAT_ID.*(ADMIN_CHAT_ID|121604706)' test.sh 2>/dev/null; then \
		echo "❌ Real chat ID in test config — TEST_CHAT_ID must never use ADMIN_CHAT_ID"; \
		grep -nE 'TEST_CHAT_ID.*(ADMIN_CHAT_ID|121604706)' test.sh | head -3; exit 1; fi
	@echo "✓ All checks passed"

cd: ## Full CD pipeline: lint → local FAST → triassic-4 cross-platform
	@echo "═══ Stage 1/3: Lint + hygiene ═══"
	@$(MAKE) check
	@echo ""
	@echo "═══ Stage 2/3: Local FAST tests ═══"
	@$(MAKE) test-fast
	@echo ""
	@echo "═══ Stage 3/3: triassic-4 cross-platform (beast host) ═══"
	@$(MAKE) test-remote
	@echo ""
	@echo "✅ CD pipeline passed — all stages green"

lint: ## Run mypy type checking
	python3 -m mypy bridge.py claudecode.py telegram.py shared.py --ignore-missing-imports
