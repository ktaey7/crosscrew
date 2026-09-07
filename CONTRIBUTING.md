# Contributing

Start with a concrete installation problem or one review/follow-up workflow.
Keep changes small: a provider adapter or lifecycle fix is more useful than a new
mandatory dashboard or orchestration framework.

Use Python 3.11+ on PATH. The runtime and tests use the standard library only.

```bash
python3 --version
python3 -m unittest discover -s tests -v
```

Tests use temporary state, fake native CLIs and local test broker processes. They
must not require subscription credentials or make real AI calls. Some enforcement
tests require macOS `sandbox-exec` outside an already sandboxed host; Linux skips
those tests. Do not interpret a skip as proof of permission enforcement.

The original personal environment had additional tests asserting user-specific
symlinks, Council text, media skills and local preferences. Those are not portable
public acceptance tests and are excluded from this extraction. Runtime adapter,
broker, lifecycle, session and artifact tests are retained. New installation tests
use an isolated HOME and never write to a developer's real host directories.

If changing the lifecycle enum, update `quick_contract.py` and regenerate:

```bash
python3 quick_contract.py > quick-contract.md
```

For a new provider, see [architecture.md](architecture.md). Do not claim supported
status from a route matrix alone. Do not add auto-login, credential import,
subscription-to-API proxies or silent billing fallbacks.

Before publishing, scan the full staged tree for credentials, personal absolute
paths, prompts and runtime state. Never attach `.state`, `.briefs`, user config
files or raw session logs to issues. Include sanitized expected/actual behavior,
OS/Python/CLI versions and the command shape instead.
