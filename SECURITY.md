# Security and safety

ContractDiff is an offline prototype using synthetic agreements. It has not been hardened or independently audited for production use. Do not use the demo to approve agreements or process confidential contracts.

## Data and credentials

The browser demo loads local JavaScript data files and does not call a model endpoint. Every fixture exported into those files is readable by anyone with the repository or a hosted copy. There is no authentication, tenant isolation or persistent audit/decision store.

Only the optional Python live-generation command sends requests externally. It sends contract text and metadata, the selected template, playbook rules and prompt/schema content to `OPENAI_BASE_URL`. Configure a trusted HTTPS endpoint and review its data-handling terms. Keep API credentials in your environment or local `.env`; never commit keys, real agreements or sensitive endpoint logs. Provider error bodies may appear in local logs.

## Trust boundaries

- Contract text is untrusted data. Prompts delimit it and instruct the model not to follow embedded instructions
- Model output is candidate data. The shared schema forbids extra fields, and validation rejects reserved severity, route and approval fields
- Supplied evidence must match an exact source slice. The pipeline repairs mechanical references before validation, including recalculating offsets and sometimes re-anchoring quotes to template clauses
- Only deterministic playbook code assigns legal severity. The router selects the more cautious route across the two paths; unsupported or failed analyses go to manual review, with duplicate detection taking precedence
- Browser rendering escapes text, and generated data exports escape characters that are unsafe in script contexts. These measures are not a substitute for production web-security review

The ordinary flow validates candidates before routing. The routing script is not a standalone full candidate validator; do not treat arbitrary or manually altered JSON as trusted merely because the router accepts it. SHA-256 hashes detect equality or inconsistency in canonical text, not provenance, authorship or tamper-proof authenticity.

## Residual risks

Verbatim evidence can accompany an incorrect conclusion. Source validation does not prove semantic equivalence, establish that a clause is absent, or guarantee that a quote supports the issue. Offset repair selects the first occurrence of repeated text. Confidence values are uncalibrated and may be wrong.

Prompt injection can still suppress or distort a semantic-only finding. The baseline's injection tripwire and the included example cover a narrow case. A deterministic router cannot recover an issue missed by both paths, and the union can over-escalate false findings. Suggestions and tracked changes need independent legal review.

## Reporting concerns

For a suspected vulnerability, prepare a minimal reproduction using synthetic data. Never include real contracts, personal data, credentials or sensitive logs in a public issue. If private reporting is available on the repository, use it for security-sensitive details.
