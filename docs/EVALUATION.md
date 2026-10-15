# Evaluation

Measured behaviour of the agent layer on a local 7B model, and what the
system does about it.

This document exists because the interesting engineering question in a
local-LLM tool is not "can an agent produce a finding" — it can, always, which
is the problem — but "how much of what it produces is true, and what does the
system do about the rest". Everything below is from real runs, not estimates.

## Setup

| | |
| --- | --- |
| Model | `qwen2.5-coder:7b`, both agents |
| Runtime | Ollama 0.40.2, local |
| Hardware | MacBook Air (Apple silicon, unified memory) |
| Decoding | JSON-Schema constrained, `temperature=0.1` |
| Sample | Two Python files — one with five planted vulnerabilities, one trivially safe |

The sample is small and the conclusions are correspondingly provisional. They
are recorded because the failure *modes* were consistent and are the kind that
a larger corpus would refine rather than reverse.

## Precision

Across the runs, the deterministic ruleset reported 5 findings and all 5 were
correct. The agent reported 4 findings and 1 was correct.

| Detector | Reported | Correct | Precision |
| --- | --- | --- | --- |
| Deterministic AST rules | 5 | 5 | 100% |
| Local 7B agent | 4 | 1 | 25% |

The one good agent finding was a corroboration of a deterministic one — an
SQL injection the scanner had already flagged, which the agent independently
described correctly, producing a `hybrid` finding. This is the behaviour the
merge step is designed to capture.

The three bad findings share a property that turns out to be useful.

## Hallucinated findings cite impossible CWEs

| Reported title | CWE cited | What that CWE actually is |
| --- | --- | --- |
| Information Disclosure in Errors | CWE-789 | Memory allocation with excessive size |
| Unsafe Default for `mode` | CWE-680 | Integer overflow to buffer overflow |
| Unsafe Defaults | CWE-787 | Out-of-bounds write |

All three are memory-corruption classes that require manual memory
management. **None is expressible in pure Python.** The single correct agent
finding cited CWE-89, SQL injection, which is both real and applicable.

The model therefore signals its own hallucinations through a CWE that the
target language cannot exhibit. That is a cheap, high-precision filter: reject
any agent finding whose CWE belongs to a class the language cannot produce. On
this sample it would have removed all three false positives and none of the
true ones.

This is the basis for a plausibility filter in the audit node, sitting
alongside the out-of-range line-number check that is already there. Both are
the same idea — the agent's output is checked against facts the system knows
independently — and both are cheaper and more reliable than trying to prompt
the behaviour away. The line-number check ships today; the CWE filter is
specified but **not yet implemented** (see [status](#implementation-status)).

## Self-reported confidence is not a signal

Every agent finding, true and false alike, was returned with
`confidence: 1.0`. The deterministic rules carry hand-assigned confidences
between 0.7 and 0.95 that reflect the actual false-positive rate of each rule.

Displaying "100% confidence" beside an invented finding is worse than
displaying nothing, because it transfers unearned authority. The intended
treatment is to keep agent-reported confidence in the API response — it is
part of the model's output and belongs in the record — and stop surfacing it
in the IDE. **Not yet implemented**; the webview still renders it.

The useful signal is not what the model says about itself; it is whether an
independent detector agrees, which is what `detector: "hybrid"` encodes.

## The refactor that quietly changed behaviour

This is the most important result here.

Given a function with planted vulnerabilities, the agent returned a rewrite
that was a genuine improvement: it parameterised the SQL query, replaced MD5
with SHA-256, dropped `shell=True` in favour of an argument list, and
flattened the nesting with a guard clause. It parsed cleanly, so the syntax
validator passed it.

It also changed what the function does:

| | Original | Proposal |
| --- | --- | --- |
| Falsy input | Returns an MD5 hash | Returns the string `'No input provided'` |
| Subprocess failure | Ignored | Raises `CalledProcessError` (`check=True` added) |

The model reported `behaviour_preserved: true` and `estimated_risk: "low"`.
Both were wrong, and nothing in the pipeline could tell.

The syntax validator is a real guard — it catches the common failure of
plausible-but-invalid code — but it is a guard against *one* failure mode, and
this is a different one. A refactor can parse perfectly and still break every
caller that relies on the old return type.

This is the entire argument for the Phase 4 test-generation agent: synthesise
tests against the original function, run them against the proposal in a
sandbox, and surface only what passes. Until that exists, proposals are
presented as suggestions requiring review, never applied without an explicit
diff and confirmation.

## Implementation status

Two of the four responses below are in the code with regression tests; two are
specified and not yet built. They are listed together because the measurement
is what produced all four, and separating them would hide that.

### Shipped

1. **Dedupe on file and line, ignoring the CWE.** An MD5 call was reported
   twice — once as CWE-327 (correct) and once as CWE-789 (impossible) — and
   the original merge key included the CWE, so both rows survived into the
   report. The key is now `(file_path, line_start)`; the curated CWE wins the
   merge. Regression test: `test_merge_collapses_same_line_despite_disagreeing_cwe`.

2. **Proposals cite only the findings they were shown.** The original code
   attributed every finding inside the target function to the proposal,
   including ones below the severity threshold that were never sent to the
   model. Regression test: `test_proposal_only_claims_findings_it_was_shown`.

### Specified, not yet built

3. **CWE plausibility filtering** in the audit node, per the table above.

4. **Agent confidence hidden in the IDE**, retained in the API.

## Reproducing

```bash
cd services/orchestrator
source .venv/bin/activate

CMS_AUDIT_MODEL=qwen2.5-coder:7b \
CMS_REFACTOR_MODEL=qwen2.5-coder:7b \
uvicorn app.main:app --reload
```

```bash
curl -s localhost:8000/api/v1/analysis/run \
  -H 'content-type: application/json' \
  -d @docs/samples/vulnerable.json | python3 -m json.tool > run.json
```

Read the output from a file rather than a terminal: proposals contain
significant leading whitespace that terminal wrapping destroys, which can make
valid code look mangled.

To compare models, vary `CMS_AUDIT_MODEL` over whatever is pulled locally and
diff the `findings` arrays for the same input.

## Timing

Observed on the MacBook Air, `qwen2.5-coder:7b` for both agents, one function:

| Node | Duration |
| --- | --- |
| `parse_ast` | 0.7 ms |
| `security_audit` | 44.6 s |
| `refactor` | 31.5 s |
| `report` | 42 ms |
| **Total** | **76 s** |

The deterministic layer is four orders of magnitude faster than the agent
layer. That ratio is why `/api/v1/analysis/parse` exists as a separate
endpoint — the IDE refreshes complexity metrics on every save from that path
and reserves the full pipeline for an explicit request — and why the streaming
endpoint is necessary rather than decorative.
