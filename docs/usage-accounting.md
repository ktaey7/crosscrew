# What usage reports measure

`crosscrew usage` reads only the explicit result files supplied to it. It does not
search your home, discover sessions, call providers or change runtime state.
Inputs must be regular JSON result envelopes with `schema_version: "2.0"`, a known
provider and a nonempty status; files larger than 2 MiB and symlinks are refused.
A job's schema 1.0 wait response is not a result envelope: use its `result_ref.path`.

```bash
crosscrew usage /absolute/path/result.json /absolute/path/result.pending.json
```

| Provider | Observed source | Aggregation |
|---|---|---|
| Claude | Result JSON usage | Per invocation; sum reported counts |
| Codex | Terminal JSONL usage event | Session cumulative; one compatible snapshot per session |
| Grok | Native session usage.json for the exact target/session, updated after launch | Session cumulative; one compatible snapshot per session |
| agy | Native JSON usage fields | Scope unverified; preserve values but exclude from totals |

Native field schemas can change. Unknown/missing values remain `null`, never zero.
The summary uses allowlisted metadata and does not print answer bodies or auth
values. Duplicate invocation identities/files count once. Cumulative sessions
without a valid UUID, with decreasing counters or incompatible partial snapshots
remain unresolved. Do not sum each resumed session snapshot: that double-counts
prior turns. Session totals may include work before the files you supplied.

Each token field reports `reported_sum` and `missing_calls`. A partial sum is not a
complete total. Failed calls remain in the report and can consume usage. Provider
token fields have different meanings and are not combined into one universal cost.

This is **provider-reported usage**, not independently verified billing, money
saved, remaining subscription allowance or an account quota estimate. Crosscrew
does not automatically collect the main AI's briefing, waiting, review or rework
cost. To compare complete workflows, explicitly provide comparable host results
covering those steps and independently judge acceptance. See
[comparison-report.md](comparison-report.md).
