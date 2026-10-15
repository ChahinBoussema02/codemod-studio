# Architecture

How the system is put together, and why each significant decision went the way
it did. The [README](../README.md) has the summary; this is the reasoning.

## The shape of the problem

A code-analysis tool built on a local 7B model has to reconcile two facts that
pull against each other:

- A small model can read code and notice things a pattern matcher cannot —
  missing authorisation, a trust boundary in the wrong place, validation that
  looks thorough but is not.
- A small model also invents findings, misattributes line numbers, assigns
  wrong identifiers, and produces code that looks right and is not. Measured
  precision on the sample corpus was 25% (see [EVALUATION.md](EVALUATION.md)).

The whole architecture is an answer to that tension. Every design choice below
is some form of: *let the model contribute where only it can, and check
everything it says against something that cannot lie.*

## Layers

```
app/
├── schemas/       domain model — no framework imports
├── analysis/      AST backends, metrics, deterministic rules
├── llm/           the only module that opens a socket for inference
├── agents/        LangGraph topology and nodes
├── telemetry/     outbound metrics
├── api/           HTTP transport
└── core/          settings, logging, error types
```

Dependencies point inward: `api` → `agents` → `analysis` + `llm` → `schemas`.
Nothing in `schemas` or `analysis` imports LangGraph or FastAPI, which is what
lets the AST engine ship as a standalone CLI with no server and no agent
framework.

## State

`AnalysisState` is a Pydantic model used *directly* as the LangGraph state
schema rather than a `TypedDict`. The consequence is that every partial update
a node returns is validated at the boundary — a malformed node output fails
loudly where it happened instead of corrupting state that surfaces three nodes
later.

Fields split into two kinds:

```python
# Accumulators: several nodes contribute, merged by reducer.
findings:  Annotated[list[SecurityFinding], operator.add]
proposals: Annotated[list[RefactorProposal], operator.add]
traces:    Annotated[list[AgentTrace], operator.add]

# Scalars: one node owns each at a time, last write wins.
phase:   AnalysisPhase
module:  ModuleProfile | None
summary: RiskSummary | None
```

Getting this wrong is the classic LangGraph bug: without the reducer, the
second node to write `findings` silently discards the first node's findings.

### Checkpointing

The graph compiles with a checkpointer, which gives run replay for free —
`GET /api/v1/analysis/{run_id}` serves a completed run from the checkpoint, so
the IDE can recover results after a window reload without re-spending a minute
of inference.

LangGraph refuses to deserialise types that are not explicitly allow-listed,
as a guard against arbitrary object construction from checkpoint data. The
allow-list is built from the state classes themselves rather than string
paths, so it survives a module rename:

```python
_CHECKPOINT_TYPES: tuple[type, ...] = (AgentTrace, AnalysisPhase, AnalysisState, ...)
```

Swapping `MemorySaver` for `AsyncPostgresSaver` is a one-line change; the
graph code does not move.

## The pipeline

```
START → parse_ast ─┬─(no code)──────────────────────────────┐
                   └→ security_audit ─┬─(nothing to fix)───┐│
                                      └→ refactor ─────────┴┴→ report → END
```

### Why sequential, not parallel

The audit and refactor agents look like obvious candidates for a fan-out. They
are not, for two reasons. The refactor agent needs the audit's findings to
rank its targets and to name the vulnerability it is closing. And Ollama
serialises inference requests anyway, so running them concurrently would
double peak VRAM on a single-GPU machine for no latency gain.

### Why the conditional edges exist

A full agent run costs 30–90 seconds on consumer hardware. Not spending it is
a feature, so two edges short-circuit:

- `route_after_parse` sends a file with no executable code straight to the
  report. New modules and scaffolded `__init__.py` files are common in an IDE.
- `route_after_audit` skips the refactor agent when nothing met the severity
  or complexity threshold, and when the file does not parse — splicing
  generated code into a file whose structure is unknown is unsafe.

### Nodes are factories

`make_security_node(llm)` and `make_refactor_node(llm)` return the node
closure with the inference client bound. The client is injected rather than
imported, which is what makes the entire graph testable: the suite passes a
`StubLlm` implementing the same narrow protocol, and no test touches a socket.

```python
class LlmClient(Protocol):
    async def structured(self, *, model, system, prompt, schema, temperature): ...
    async def available_models(self) -> list[str]: ...
    async def ping(self) -> bool: ...
```

Three methods. A narrow protocol is what makes the stub honest — there is no
surface area where the fake can diverge from the real client.

### Nodes never raise

Each node catches its own exceptions and records them on its trace as
`degraded` or `failed`. One agent failing should downgrade the report, not
destroy the findings earlier agents already produced. This is why an
unreachable Ollama yields a complete deterministic report with a warning
rather than a 500.

## Detection

### Deterministic rules

AST-level rules in `analysis/security_rules.py`, each carrying a CWE, an OWASP
category and a concrete remediation. Rules operate on the parsed tree rather
than on text, which keeps false positives low — `subprocess.run` with
`shell=True` is a finding; the same call with an argument list is not, and a
regex cannot tell them apart reliably.

A smaller set of text-level rules (private keys, AWS key IDs) runs on raw
source, so they still apply to files that fail to parse and to languages with
no AST backend yet.

### Merge

Findings from both detectors are merged on `(file_path, line_start)`. When two
detectors report the same line, the curated finding wins on wording and CWE,
the higher severity is kept, confidence is raised, and the result is marked
`hybrid`.

The CWE is deliberately **not** part of the merge key. Including it was the
original design and it failed in practice: an MD5 call came back as CWE-327
from the scanner and CWE-789 from the agent, so one issue became two rows.
Model-assigned CWEs are not reliable enough to key on.

### Validation of generated code

Two checks stand between the model and the user:

1. **Line plausibility.** A finding pointing past the end of the file is
   dropped. A finding the user cannot navigate to erodes trust in every other
   finding.
2. **Syntax.** Every refactor proposal is re-parsed by the AST backend, after
   dedenting to module level so a method body parses standalone. Anything that
   fails is discarded with a warning.

Neither check can catch a semantic change — a refactor that parses perfectly
can still break callers. That gap is documented in
[EVALUATION.md](EVALUATION.md) and is what the Phase 4 test-generation agent
is for.

## Inference

`llm/ollama.py` is the only module in the service that opens a socket for
inference. That is deliberate: it makes the zero-leakage property auditable by
reading one file.

### Schema-constrained decoding

Ollama's `format` parameter accepts a JSON Schema and constrains generation at
the sampler, so the model cannot emit output that violates it. Pydantic models
serve as both the constraint and the validator:

```python
body = {"format": schema.model_json_schema(), ...}
return schema.model_validate_json(strip_reasoning(result.content))
```

On a validation failure the client re-prompts with the validator's own error
message, which recovers most near-misses within one retry.

`strip_reasoning` removes `<think>…</think>` spans so reasoning models
(DeepSeek-R1) work without a separate code path.

### Prompt schemas are flatter than domain schemas

The models in `llm/prompts.py` are deliberately simpler than the domain models
they feed. Constrained decoding on a 7B model degrades quickly with nesting
depth and optional unions, so the model is asked for the minimum it actually
knows, and the node enriches the result with everything deterministic — file
path, enclosing symbol, detector provenance.

### The offline guarantee

`_assert_local` rejects any inference base URL that is not loopback or a
private address when `CMS_OFFLINE_ONLY` is set, which it is by default. A
misconfigured endpoint pointing at a hosted API is a startup failure, not a
silent exfiltration path.

## Telemetry

The .NET service receives metrics only: a truncated SHA-256 of the file path,
counts, durations, grades and token totals. No source, no finding text, no
file names. Hashing the path preserves the ability to count repeat analyses of
the same file without making the file identifiable.

Ingestion is idempotent on `run_id` — the orchestrator may retry after a
transient failure, and an upsert is friendlier than making the caller handle a
conflict. Telemetry failures are logged and swallowed; the analysis never
depends on them.

## The IDE client

Findings go to the native Problems panel rather than a custom tree, so they
inherit keyboard navigation, filtering and `Go to next problem` for free. The
webview renders the live agent timeline, which exists because 76 seconds of
silence reads as a hang.

The webview CSP allows exactly one nonce-tagged script and no network access,
and every value originating from a model is inserted with `textContent`, never
`innerHTML`. Model output is untrusted input, and this is a context with
script privileges.

Proposals are never applied silently: the user sees a diff and confirms, and
the edit goes through a `WorkspaceEdit` so a single undo reverses it.

## Extension points

- **A new language** implements `SourceAnalyzer` and calls `register()`. The
  graph nodes never learn which backend they are talking to.
- **A new deterministic rule** is a visitor method plus a `_Hit`.
- **A new agent** is a node factory, a constrained output schema and an edge.
- **A durable checkpointer** is a constructor argument to `build_graph`.
