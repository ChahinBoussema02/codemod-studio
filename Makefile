.DEFAULT_GOAL := help
ORCH := services/orchestrator
EXT  := clients/vscode-extension

.PHONY: help setup dev up down logs test lint typecheck models analyze clean

help: ## Show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) \
		| awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-12s\033[0m %s\n", $$1, $$2}'

setup: ## Install Python and extension dev dependencies
	cd $(ORCH) && pip install -e ".[dev]"
	cd $(EXT) && npm install

dev: ## Run the orchestrator with autoreload (needs Ollama on :11434)
	cd $(ORCH) && uvicorn app.main:app --reload --port 8000

up: ## Start the whole stack in Docker
	docker compose up --build -d
	@echo "Orchestrator  http://localhost:8000/docs"
	@echo "Telemetry     http://localhost:8080/health"

down: ## Stop the stack (volumes preserved)
	docker compose down

logs: ## Tail orchestrator logs
	docker compose logs -f orchestrator

test: ## Run the Python test suite
	cd $(ORCH) && pytest -q

lint: ## Lint and format-check
	cd $(ORCH) && ruff check . && ruff format --check .
	cd $(EXT) && npm run lint

typecheck: ## Static type analysis for both languages
	cd $(ORCH) && mypy app
	cd $(EXT) && npx tsc -p ./ --noEmit

models: ## Pull the default Ollama models
	ollama pull qwen2.5-coder:7b
	ollama pull qwen2.5-coder:14b

analyze: ## Dogfood: run the AST profiler over the orchestrator itself
	cd $(ORCH) && python scripts/analyze_file.py app --min-complexity 6 --security

clean: ## Remove build and cache artefacts
	find . -type d -name __pycache__ -prune -exec rm -rf {} +
	find . -type d -name .pytest_cache -prune -exec rm -rf {} +
	rm -rf $(EXT)/out $(EXT)/node_modules
