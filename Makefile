# Makefile for frappe-microservice-lib
# ── Developer Setup ──────────────────────────────────────────────────────────
.PHONY: setup lint test

setup: ## Install pre-commit hooks (run once after cloning)
	@echo "Installing pre-commit hooks..."
	@command -v pre-commit >/dev/null 2>&1 || pip install pre-commit
	@pre-commit install
	@echo "✅ Pre-commit hooks installed. They will run automatically on git commit."

lint: ## Run all pre-commit checks manually
	pre-commit run --all-files
