# Evaluation

## What this measures

This is an authored **11-document synthetic smoke test**, including a duplicate and a linked revision pair. The examples exercise the pipeline and make specific failures inspectable. They are not held out from development; the rules, prompts and examples were developed together. These results do not estimate performance on unseen agreements.

The checked-in labels are in [`data/expected_labels.json`](data/expected_labels.json). [`evaluation/evaluate.py`](evaluation/evaluate.py) compares them with the saved AI candidates, baseline candidates and policy-enforced union. Per-document results are available as [`results.json`](evaluation/results.json), [`results.csv`](evaluation/results.csv) and the browser's Evaluation view.

## Saved snapshot

| Analysis path | Labelled issues recovered | Misses | False findings | Exact issue sets |
| --- | ---: | ---: | ---: | ---: |
| AI | 6 / 7 | 1 | 4 | 8 / 11 |
| Baseline | 6 / 7 | 1 | 0 | 10 / 11 |
| Union | 7 / 7 | 0 | 4 | 9 / 11 |

The final union-based workflow matches **10 of 11 expected routes**. These denominators are different: seven labelled issue occurrences span the documents, while exact issue-set and route matches are counted per document.

- A **hit** is an expected `candidate_issue` present in a document's actual issue set
- A **miss** is an expected issue absent from that set
- A **false finding** is an issue present but absent from the labels
- An **exact issue set** has neither misses nor false findings for that document
- A **route match** compares `routing.json`'s final route with the expected route

Issues are deduplicated by type within each document. Scoring does not independently grade reasoning, redline quality, evidence relevance or clause-level completeness. `prompt_injection_detected` is an operational signal and is excluded from legal-issue scoring. Union scoring also excludes findings without a playbook-assigned severity.

## Failure cases that matter

- **NDA-002:** the saved AI run raises three false findings on semantically equivalent wording. The union preserves them, escalating a labelled `quick_check` document to `senior_legal_review`
- **NDA-005:** the AI misses an absent termination clause; the structural baseline recovers it
- **NDA-008:** the baseline misses reworded unlimited liability by design; AI recovers it but also raises a false missing-termination finding. The high-severity liability issue already determines the final route
- **NDA-006:** a single embedded prompt-injection example is surfaced separately while the liability issue is retained. This does not demonstrate general injection resistance

The union improves recall on these cases by retaining either path's candidates. It also retains the AI's false findings. A correct route can therefore conceal an incorrect issue set.

## Provenance and reproduction

The saved audit fields identify `openai/gpt-4o-mini`, prompt version `contract-review.v1` and the historical request metadata. Public fixtures and saved output text were rebranded with same-length synthetic naming, preserving quote offsets; no model call was made for that transformation. Deterministic hashes, baseline output, routing and evaluation were rebuilt. Usage, latency and cost remain historical measurements, not current provider estimates.

Follow the offline reconstruction commands in [README.md](README.md). To recalculate the evaluation from the current analysis records and routing:

```sh
python3 evaluation/evaluate.py
python3 pipeline/export_browser_data.py
```

A live rerun is a new experiment, not a deterministic reproduction of the saved model responses. Record its model, prompt, inputs, output-validation failures and audit data together. Broadening the evidence would require independent contracts and labels, varied clause language, unsupported formats, richer adversarial cases and repeated model runs before making any reliability claim.
