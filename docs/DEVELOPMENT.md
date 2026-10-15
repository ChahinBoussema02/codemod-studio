# Development

Setup, the test strategy, and the problems you are most likely to hit.

## Prerequisites

| | |
| --- | --- |
| Python | 3.11+ |
| Node | 20+ (extension only) |
| .NET SDK | 9.0+ (telemetry service only) |
| Ollama | any recent version, for the agent layer |
| Docker | optional, for the full stack |

Only Python is needed to run the tests — the suite stubs the inference client
and never opens a socket.

## Orchestrator

```bash
cd services/orchestrator
python -m venv .venv && source .venv/bin/activate   # Windows: .venv\Scripts\activate
pip install -e ".[dev]"

pytest -q
uvicorn app.main:app --reload                       # http://localhost:8000/docs
```

The service starts whether or not Ollama is running. Without it, the agent
nodes report `degraded` and the deterministic rules still produce a full
report — which is also the fastest way to work on the non-agent parts.

### Configuration

Every setting is an environment variable prefixed `CMS_`; see
[`.env.example`](../.env.example). The ones that matter:

| Variable | Default | Purpose |
| --- | --- | --- |
| `CMS_OLLAMA_BASE_URL` | `http://localhost:11434` | Inference host |
| `CMS_AUDIT_MODEL` | `qwen2.5-coder:7b` | Security auditing agent |
| `CMS_REFACTOR_MODEL` | `qwen2.5-coder:14b` | Refactoring agent |
| `CMS_OFFLINE_ONLY` | `true` | Refuse non-local inference endpoints |
| `CMS_MAX_HOTSPOTS_PER_RUN` | `5` | Cap on functions sent to the refactor agent |

On a laptop, point both agents at the 7B — the 14B is roughly 9 GB and slow on
shared memory:

```bash
CMS_AUDIT_MODEL=qwen2.5-coder:7b \
CMS_REFACTOR_MODEL=qwen2.5-coder:7b \
uvicorn app.main:app --reload
```

## The AST engine standalone

No server, no LangGraph, no Ollama:

```bash
python scripts/analyze_file.py app --min-complexity 6 --security
python scripts/analyze_file.py app --fail-over 15      # exit 1 when breached
python scripts/analyze_file.py app/agents --json
```

The `--fail-over` form is what CI runs over this repository's own source.

## VS Code extension

```bash
cd clients/vscode-extension
npm install
npm run watch        # leave running; recompiles on save
```

Create `.vscode/launch.json` (not committed, since it is per-developer):

```json
{
  "version": "0.2.0",
  "configurations": [
    {
      "name": "Run Extension",
      "type": "extensionHost",
      "request": "launch",
      "args": ["--extensionDevelopmentPath=${workspaceFolder}"],
      "outFiles": ["${workspaceFolder}/out/**/*.js"]
    }
  ]
}
```

Open **`clients/vscode-extension` itself** as the workspace folder — not the
monorepo root, or VS Code will not find that config — then press `F5`. A second
window titled `[Extension Development Host]` opens with the extension loaded.

In that window, open a Python file and run **CodeMod Studio: Check
orchestrator and Ollama status** first; it is the cheapest confirmation that
the two processes are talking. Then **Run full agent analysis**.

## Telemetry service

```bash
cd services/telemetry-api
dotnet restore
dotnet run                      # http://localhost:8080/health
```

It needs PostgreSQL. The quickest way is the compose stack, or:

```bash
docker run --rm -p 5432:5432 \
  -e POSTGRES_USER=cms -e POSTGRES_PASSWORD=cms -e POSTGRES_DB=cms_telemetry \
  postgres:16-alpine
```

The schema is created with `EnsureCreated` so `docker compose up` stays a
single command. Phase 3 replaces that with versioned migrations once the
schema holds data worth preserving.

## Full stack

```bash
cp .env.example .env
docker compose up --build
```

| Service | Address |
| --- | --- |
| Orchestrator | http://localhost:8000/docs |
| Telemetry | http://localhost:8080/health |
| PostgreSQL | localhost:5432 |

Ollama stays on the host by default — most developers already have models
pulled, and on macOS a containerised Ollama gets no GPU access. To containerise
it anyway: `docker compose --profile ollama up`.

## Testing

```bash
pytest -q                            # all
pytest -q tests/test_python_ast.py   # analyzer and rules
pytest -q tests/test_graph.py        # pipeline end to end
pytest -q -k "merge or dedupe"       # by name
pytest -q --cov=app --cov-report=term-missing
```

The suite runs the **real compiled graph** — real routing, real state
reducers, real validation — against a deterministic `StubLlm` implementing the
same protocol as the production client. Nothing is mocked at the HTTP layer,
nothing sleeps, and no test needs Ollama.

Two consequences worth knowing when adding tests:

- A canned agent response goes in `conftest.py` as a fixture, not inline. The
  existing `silent_llm` and `opinionated_llm` cover the "finds nothing" and
  "finds something and proposes a fix" cases.
- Any bug found against a real model should land as a test with a docstring
  naming the observed behaviour. `test_merge_collapses_same_line_despite_disagreeing_cwe`
  and `test_proposal_only_claims_findings_it_was_shown` both came from real
  runs; each fails against the code as it was before the fix.

## Quality gates

```bash
make lint        # ruff check + ruff format --check, eslint
make typecheck   # mypy --strict, tsc --strict
make test
```

All three run in CI, plus the self-analysis complexity gate and Docker image
builds. `mypy --strict` passes with one documented suppression: LangGraph's
`add_node` overloads declare their parameter as `_Node[Never]`, which no
concrete callable type satisfies.

## Troubleshooting

**`Error: listen tcp 127.0.0.1:11434: bind: address already in use`**
Ollama is already running — the macOS app starts it automatically. Check with
`curl -s localhost:11434/api/version`; do not run `ollama serve`.

**`/health/ready` says ready but a run 404s on the model**
Tags must match exactly. `qwen2.5-coder:7b` does not satisfy a configured
`qwen2.5-coder:14b`. `ollama list` shows what is actually pulled.

**Proposals look like mangled code in the terminal**
Terminal wrapping destroys the leading whitespace. Redirect to a file and read
it there. `syntax_validated: true` means the stored code parsed server-side.

**Findings all show `detector: "heuristic"`, none from the agent**
The agent leg degraded. Check `warnings[]` in the response and the trace
status for `security_audit` — the usual cause is Ollama not running or the
configured model not pulled.

**`ModuleNotFoundError: No module named 'pydantic'`**
The virtualenv is not active. `source .venv/bin/activate`; the prompt should
show `(.venv)`.

**A run takes 70+ seconds**
Expected for the full pipeline on CPU. Use `/api/v1/analysis/parse` for
metrics only (milliseconds), or `/stream` to see findings as they arrive.

## Adding a language

1. Implement `SourceAnalyzer` in `app/analysis/` — `analyze()` and
   `is_valid_syntax()`. Never raise on malformed input; set
   `ModuleProfile.syntax_error` instead.
2. Declare the languages it handles in `languages`.
3. `register()` it in `app/analysis/registry.py`.

The graph nodes need no change. Only `scan_python` is Python-specific; other
languages get the text-level rules until a dedicated ruleset exists.

## Adding a deterministic rule

A visitor method in `_PythonSecurityVisitor` plus a `_Hit` carrying a rule id,
CWE, OWASP category and a concrete remediation — "use bound parameters", not
"validate input". Add a positive and a negative test; the negative matters
more, since a noisy rule is worse than a missing one.
