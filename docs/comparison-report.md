# Compare explicit Codex-host runs

This optional report compares Codex host token vectors for two independently
accepted runs. It does not run an experiment or read native Codex session logs.
Supply complete schema 2.0 Crosscrew result envelopes as described in
[usage-accounting.md](usage-accounting.md). Other providers can appear as workers;
non-Codex host results make the host comparison ineligible.

Create a manifest; paths are relative to the manifest unless absolute:

```json
{
  "schema_version": 1,
  "direct": {
    "accepted": true,
    "host_results": ["direct-host.json"],
    "worker_results": []
  },
  "delegated": {
    "accepted": true,
    "host_results": ["delegated-host.json"],
    "worker_results": ["worker-result.json"]
  }
}
```

```bash
crosscrew compare comparison.json
```

Set `accepted` only after independent review of the actual output and acceptance
criteria. A successful provider status is insufficient. Include the full host
preparation and review/rework scope; file omission is not something this tool can
detect. Use separate comparable sessions so prior work does not distort cumulative
counters. Crosscrew does not generate these host-side measurements automatically.

The report keeps host/worker summaries separate. Complete, accepted arms can
produce differences for uncached input, cached input and output tokens. A zero
baseline has no percentage. Negative savings mean more observed host tokens.
It never converts them to dollars or remaining subscription quota. Less host usage
can coincide with more worker usage, latency or user intervention.

Malformed manifests and files shared across arms/roles are rejected. Incomplete
host usage, rejected output, invalid worker files or worker failures suppress
savings and return reasons. Missing worker usage remains visible in its separate
summary; the comparison does not establish total workflow efficiency.

Exit codes: eligible report `0`, ineligible report `1`, invalid manifest `2`.
Only the supplied files are read. Raw answers, credentials and personal paths are
not copied into the report.
