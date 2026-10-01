# ContractDiff

An evidence-led contract triage prototype: compare synthetic agreements with approved templates, surface candidate issues, and route them through a deterministic legal playbook.

**AI proposes findings. The playbook assigns severity. A human decides.**

The browser demo is plain HTML, CSS and JavaScript. It replays saved Python pipeline outputs entirely offline: no backend, build step, account or API key is needed.

## Try it in five minutes

Open [`prototype/index.html`](prototype/index.html) in a browser. Keep the repository's folder structure intact so its local scripts can load. Alternatively, serve it locally:

```sh
python3 -m http.server 8000 --bind 127.0.0.1
```

Then visit <http://127.0.0.1:8000/prototype/>.

1. Open **NDA-003** for a liability finding: source evidence, template language, playbook rule and suggested redline appear together
2. Compare **NDA-007 → NDA-008** for the revision example: previously rejected liability language returns in wording the baseline misses
3. Open **NDA-006** for the embedded prompt-injection example and its separate operational warning
4. Inspect **NDA-002**, a semantically equivalent rewrite that the saved AI run wrongly escalates
5. Open **Evaluation** to compare AI, baseline and union results, including their errors

Accept, Edit, Reject and Escalate are illustrative UI actions. They do not save decisions or approve a contract. The generated [`redline/nda_003_redline.docx`](redline/nda_003_redline.docx) contains a real Word tracked insertion and deletion for the single NDA-003 example.

## What is implemented

- A queue, evidence-led review screen and per-document evaluation view
- Two analysis paths: saved LLM candidates and a keyword/structural baseline
- Shared schema, reference and verbatim-evidence validation
- Deterministic playbook enforcement and conservative routing across both paths
- Duplicate detection by SHA-256 of canonical contract text
- One linked revision example and one reproducible tracked-changes DOCX
- Versioned prompts, schemas and recorded generation metadata

The sample contains 11 synthetic documents, including one duplicate and a linked revision pair, with two templates and two playbooks. Supported fixtures are mutual NDAs and a customer order form; an unsupported document demonstrates manual routing.

## Architecture and trust boundary

```text
Authored fixtures + templates + playbooks
                 |
       +---------+---------+
       |                   |
  LLM candidates     Rules-based candidates
       |                   |
  Mechanical repair        |
       +---------+---------+
                 |
  Shared schema / reference / evidence validation
                 |
  Playbook severity + per-path routing
                 |
  Union of issues + more cautious final route
                 |
       JSON artifacts → offline review UI
```

The model's output schema has no severity, route or approval fields. The validator rejects those reserved fields, checks document/template references, and verifies every supplied quote against its exact source slice using zero-based, end-exclusive Python character offsets. Template offsets are relative to the individual template clause; contract offsets are relative to the full contract text.

Before validation, the AI pipeline repairs mechanical references: it recalculates offsets from exact quotes, can re-anchor a quote to a matching template clause, and normalises clause IDs. It does not add or remove candidate findings during this step. If a quote appears more than once, offset repair selects its first occurrence. These checks establish that quoted text exists, **not** that the legal interpretation, clause mapping or claimed absence is correct.

`pipeline/apply_playbook.py` adds severity only where an issue matches a playbook rule. Uncovered issues remain visible without legal severity. `pipeline/route_contract.py` combines the two paths using the route requiring more human review; `data/routing.json` is the UI's source for severity and routing. Unsupported documents and failed analyses route to manual review, except that duplicate detection takes precedence. A `quick_check` route still requires a human check. The model's confidence value can trigger extra review; it is not a calibrated probability.

## Results, with limits

On this **authored 11-document smoke-test suite**, the union recovers 7 of 7 labelled legal issues, raises 4 false findings, matches the exact expected issue set on 9 of 11 documents, and routes 10 of 11 as labelled.

This is not a held-out test or a legal-performance benchmark. The fixtures and rules were developed together, and one example deliberately exposes a baseline miss. See [EVALUATION.md](EVALUATION.md) for definitions, path comparisons and failure cases.

### Saved-run provenance

`data/model_outputs.json` contains saved, pipeline-processed outputs whose audit metadata records `openai/gpt-4o-mini` and prompt version `contract-review.v1`. The public fixtures and saved outputs use the synthetic party name **ExampleCorp**, substituted at the same string length to preserve evidence offsets. Contract hashes and downstream deterministic artifacts were regenerated for that text. No new model inference was run for this public version.

The saved model IDs, timestamps, token usage, latency and cost describe the earlier recorded requests. They are not measurements of a fresh run on the rebranded fixtures or a promise of current pricing or reproducibility. Saved outputs are validated application records, not an archive of raw provider responses.

## Development and verification

Use Python 3.9 or newer and Node.js for the JavaScript checks. There is no npm dependency installation. Run from the repository root:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install -r requirements-dev.txt
python3 pipeline/validate_data.py
node --version
python3 -m pytest tests -q
```

The suite covers validation, prompts, the mocked LLM client, generation, baseline rules, playbook application, routing, browser exports, UI logic/markup, evaluation and redline generation. UI tests executed through Node are skipped if Node is absent; they are not full browser end-to-end tests. Neither these checks nor the offline demo needs a model call.

### Reconstruct local artifacts without inference

The following replaces generated files in the checkout. It uses the saved model outputs and makes no model requests:

```sh
python3 pipeline/assemble_contracts.py
python3 pipeline/compute_hashes.py
python3 pipeline/validate_data.py
python3 pipeline/baseline_rules.py
python3 pipeline/route_contract.py
python3 evaluation/evaluate.py
python3 pipeline/export_browser_data.py
python3 pipeline/build_redline.py
python3 -m pytest tests -q
```

The contract fragments, templates, playbooks, labels and saved model outputs are inputs. Do not hand-edit the browser's `prototype/data/*.js` exports. Baseline timestamps and timing metadata can change on reconstruction even when findings and routes are unchanged.

### Optional live model run

A live run transmits each contract's text and metadata, the selected template and playbook rules, and prompt/schema content to your configured provider. It may incur charges. Use synthetic data only, review the provider's terms, and keep credentials out of version control.

Copy `.env.example` to `.env` and configure these variables locally, or set them in your shell. Shell variables take precedence over `.env`:

| Variable | Purpose |
| --- | --- |
| `OPENAI_BASE_URL` | OpenAI-compatible base URL; `/chat/completions` is appended |
| `OPENAI_API_KEY` | Your provider credential |
| `OPENAI_MODEL` | Model identifier accepted by that provider |
| `OPENAI_TEMPERATURE` | Sampling temperature, default `0` |
| `OPENAI_TIMEOUT_SECONDS` | Request timeout, default `60` |

```sh
cp .env.example .env
# Fill in the local configuration before continuing.
python3 pipeline/generate_outputs.py --output live-results/model_outputs.json
```

This keeps the committed replay snapshot intact. The command processes all 11 records, with at most two model attempts per record when response validation fails. Failed analyses are written as failure markers; inspect output status even if the command exits successfully. Without complete required configuration, it skips live generation and validates existing outputs at the selected output path, if any. A new run can produce different findings.

To deliberately replace the demo's saved AI snapshot, use the default output path instead, then rerun routing, evaluation, browser export, redline generation and tests. Retain the changed audit metadata with the new results.

## Repository map

- `data/` — synthetic fixtures, templates, playbooks, labels and analysis records
- `pipeline/` — generation, validation, deterministic policy, routing and exports
- `pipeline/prompts/`, `pipeline/schemas/` — versioned model contract
- `prototype/` — offline browser interface and generated data scripts
- `evaluation/` — scoring code and JSON/CSV results
- `redline/` — generated NDA-003 tracked-changes example
- `tests/` — executable checks

## Scope

This is a review-workflow prototype, not legal advice or a production contract-management system. It has no document upload/parser, OCR, authentication, persistent decision store, general-purpose revision diff or autonomous approval. The redline generator is specific to NDA-003. Deployment with real agreements would need a separately evaluated extraction layer, access controls, data-handling safeguards and broader independent legal evaluation. See [SECURITY.md](SECURITY.md) for the threat model and residual risks.
