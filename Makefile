SHELL := bash
.SHELLFLAGS := -eu -o pipefail -c
MAKEFLAGS += --warn-undefined-variables
MAKEFLAGS += --no-builtin-rules

.PHONY: help
help: ## Show this help
	@grep -hE '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  %-20s %s\n", $$1, $$2}'

# =============================================================================
# Setup
# =============================================================================

.PHONY: install
install: ## Install dependencies (incl. dev + test; default-groups is [])
	@uv sync --group dev --group test --extra cache

# =============================================================================
# Development
# =============================================================================

.PHONY: format
format: ## Apply code formatting (black, isort)
	@uv run black .
	@uv run isort .

.PHONY: lint
lint: ## Check formatting and linting without modifying
	@uv run black --check .
	@uv run isort --check-only .
	@uv run ruff check .

.PHONY: test
test: ## Run tests
	@uv run pytest

.PHONY: test-cov
test-cov: ## Run tests with coverage
	@uv run pytest --cov --cov-report=term-missing

.PHONY: check
check: lint test ## Run lint + test

.PHONY: clean
clean: ## Remove build artifacts
	@find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	@rm -rf dist build .pytest_cache .ruff_cache .coverage htmlcov

# =============================================================================
# Security
# =============================================================================

.PHONY: security
security: security-bandit security-deps ## Run security checks

.PHONY: security-bandit
security-bandit: ## Run bandit security linter
	@uv run bandit -c pyproject.toml -r src/ -ll

.PHONY: security-deps
security-deps: ## Check dependency CVEs (uv-native, OSV-backed)
	@uv audit
