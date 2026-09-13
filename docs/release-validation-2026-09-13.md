# 0.2.0 alpha validation — 2026-09-13

This release was prepared in a separate public checkout. The maintainer's personal
runtime, installed skills, native settings and existing services were not updated.

## Public release checks

- **524 standard-library tests passed** on macOS with Python 3.14.6, including real
  OS write-enforcement tests run outside a nested host sandbox. Tests use fake
  provider CLIs and temporary state; no subscription/model calls were made.
- A real loopback test broker carried **12 cross-provider routes × fresh/resume =
  24 synthetic jobs**. Tests verified startup acceptance, session continuity, brief
  recall, result metadata and byte-size/SHA-256 matches. These are fake-provider
  transport checks, not native AI quality or all-app compatibility certification.
- Focused coverage includes Gemini missing/mismatched IDs and tool denial, literal
  command grants, cleanup after setup failure/cancel, non-mutating waits, bounded
  summaries, batch waits, broker recursion, session locks/startup observation,
  cumulative usage deduplication and incomplete comparison inputs.
- Additional installed-CLI smoke check in a new temporary HOME: install all three
  named host entrypoints; render/install/check an isolated Grok work profile;
  start a temporary broker; run doctor; complete fake-Claude fresh/implicit-resume
  reviews; verify same-byte result hashes and usage; uninstall the entrypoints.
  The test broker and temporary files were then removed. No persistent service
  was installed. Uninstall preserved state and the explicitly installed native
  profile until the test-owned temporary HOME was removed.
- Existing personal/public local checkouts were compared with a pre-work baseline:
  tracked/untracked source-file hashes, HEAD and git status remained unchanged.
- GitHub CI runs the public suite on macOS/Python 3.11 and 3.13. Check the
  [workflow runs](https://github.com/ktaey7/crosscrew/actions/workflows/tests.yml)
  for the outcome at the release commit; local and CI counts are separate evidence.

## Evidence this does not replace

The September 7 public Codex → Claude native review/re-review example remains a
historical test of 0.1.0-alpha.3. It was not rerun with paid providers for this
release. Later personal-runtime validation informed the port, but its test count,
live provider calls and local configuration are not public-release validation.

Open limits include real app-specific notifications/reconnection, a Grok hook
execution canary, native work compatibility across runtime installations,
long-running authentication expiry and any future MCP interface sandbox boundary.
The release does not introduce an MCP interface or replace the supervisor/adapter/
broker structure. It does not automatically wake a Codex conversation.

Grok/agy remain experimental. Native CLI versions, login methods, installed hooks
and permissions may change behavior. Usage reports are provider counters and
explicit-file comparisons; they do not prove account quota savings, complete
main-AI briefing/review cost, task quality or reduced total workflow effort.
