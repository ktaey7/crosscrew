# Changelog

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
