# Orchestrator

FastAPI service hosting the LangGraph analysis pipeline. See the
[repository README](../../README.md) for architecture and the full stack.

## Run

```bash
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
uvicorn app.main:app --reload                       # http://localhost:8000/docs
```

The service starts whether or not Ollama is running. Without it, the LLM
nodes report `degraded` and the deterministic security rules still produce a
full report.

## Checks

```bash
pytest -q                 # 46 tests, no network required
ruff check . && ruff format --check .
mypy app
```

## Standalone AST profiler

No server, no LangGraph, no Ollama:

```bash
python scripts/analyze_file.py app --min-complexity 6 --security
python scripts/analyze_file.py app --fail-over 12     # exit 1 when breached
python scripts/analyze_file.py app/agents --json
```

## Layout

| Path | Contents |
| --- | --- |
| `app/schemas/agent_state.py` | The LangGraph state model and domain types |
| `app/analysis/` | AST backend, complexity metrics, deterministic rules |
| `app/llm/` | Ollama client and schema-constrained prompts |
| `app/agents/` | `graph.py` plus one module per node |
| `app/api/` | Routers and dependency wiring |
| `tests/` | Graph and analyzer tests against a stubbed LLM |

## Configuration

Every setting is an environment variable prefixed `CMS_`; see
[`.env.example`](../../.env.example). The ones that matter most:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CMS_OLLAMA_BASE_URL` | `http://localhost:11434` | Inference host |
| `CMS_AUDIT_MODEL` | `qwen2.5-coder:7b` | Security auditing agent |
| `CMS_REFACTOR_MODEL` | `qwen2.5-coder:14b` | Refactoring agent |
| `CMS_OFFLINE_ONLY` | `true` | Refuse non-local inference endpoints |
