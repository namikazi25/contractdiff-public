# JSON schema contracts

These schemas use JSON Schema Draft 2020-12 and define three versioned
contracts:

- `findings_v1.schema.json`: the complete pre-enforcement output shared by
  the AI and baseline paths.
- `analysis_meta_v1.schema.json`: the audit object embedded in that output.
- `evaluation_result_v1.schema.json`: one per-document evaluation record.

`findings.v1` is intentionally a candidate-output contract. It has no
`severity` or `route` property, and every object is closed with
`additionalProperties: false`. Playbook enforcement is the only component allowed to enrich
candidate findings with playbook severity and routing.

Schema validation checks shape, closed vocabularies, required fields, and
numeric bounds. The custom validator remains responsible for checks
that need canonical data, including known IDs, evidence quotes and spans,
cross-document ownership, and approval claims hidden in free text.

The schemas identify each other by their `$id`. Consumers should load all
three files into the same local schema registry; they do not require network
access.
