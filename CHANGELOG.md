# Changelog

## 0.2.0a1 — 2026-09-13

This alpha changes normal host routing and Grok work setup. Review
[upgrade instructions](docs/usage.md#update-and-remove) before updating.

- Route all twelve cross-provider combinations through the typed loopback broker;
  self-delegation and recursive broker escalation remain refused. Missing/stale
  brokers fail explicitly; doctor now reports them as attention.
- Add Gemini JSON response/usage parsing and fresh/resume conversation IDs, with
  missing/mismatched IDs and headless tool denials reported as failures. Preserve
  native authentication and unpinned model defaults.
- Add read-only `wait --summary` with an 8 KiB answer, session and usage from the
  same hashed result bytes, plus single-output `wait-many` for up to sixteen jobs.
  Timeout and waiter interruption never cancel workers.
- Add explicit Grok strict work-profile setup with opt-in read-only runtime paths;
  preserve existing profiles and refuse conflicts. A native model-list preflight
  may refresh a recognized expiring login before work; no credentials are exported.
- Add temporary, escaped/anchored Gemini work command grants under outer OS write
  confinement. Use a separate `crosscrew-work-*` namespace; release grants after
  completion, cancellation and sandbox setup failure.
- Harden result/error parsing, UTF-8 handling, startup acceptance and session/job
  observation; keep host approvals separate from worker permissions.
- Add provider usage summaries and explicit independently accepted Codex-host
  comparisons. Deduplicate cumulative session counters; exclude unverified Gemini
  totals; do not claim automatic host-cost capture or subscription quota savings.
- Update English/Korean onboarding and focused approval, work and measurement docs.

See [public release validation](docs/release-validation-2026-09-13.md) for actual
checks and the distinction between synthetic routes and live provider evidence.

## 0.1.0a3 — 2026-09-07

- Return exit code 0 when the broker successfully starts a job and reports `running`, including fresh and resumed review sessions. Worker completion still requires `job wait`.
- Add a regression test that runs the public CLI through a temporary broker and checks both startup exit codes, completion and session continuity using a fake native CLI.

## 0.1.0a2 — 2026-09-07

- Bind the loopback broker without reverse DNS, removing an unnecessary startup dependency on the host resolver.
- Wait for the fake media worker before removing its test state.
- Use temporary image storage in the missing-session test instead of the maintainer's installed Codex directory.

Validation: 401 tests passed locally on macOS/Python 3.14.6.

## 0.1.0a1 — 2026-09-07

First public alpha extracted from a working personal delegation setup.

- Native CLI adapter, asynchronous jobs, persistent sessions and loopback broker.
- Read-only wait with result path/size/SHA, pending-result support and bounded
  progress metadata; waiting is distinct from worker cancellation.
- Separate Crosscrew state, environment-variable namespace and service label.
- Source installation with selected host entrypoints and collision checks;
  uninstall retains runtime state and does not remove unrelated settings.
- Optional broker installation; generated LaunchAgent can be reviewed first.
- Known API-auth override checks, with explicit limits on billing verification.
- English/Korean onboarding; Council remains a separate optional methodology.

Validation: 400 standard-library tests passed on macOS with Python 3.14.6,
including fake CLI and local broker tests and Seatbelt write enforcement. Native
AI subscriptions were not called during this public extraction. This is not
clean-machine or all-host compatibility certification. CI additionally targets
Python 3.11 and 3.13 on macOS; see each workflow run for its actual outcome.
