# Security

Do not include tokens, private briefs, native CLI configs or full runtime state in
public issues. For a suspected security defect, use GitHub's private vulnerability
reporting if available; otherwise ask the maintainer for a private contact without
including exploit details or secrets.

Crosscrew launches native CLIs with the user's authority. Review is not uniformly
OS-enforced; Claude uses a prompt guard. The optional broker intentionally runs
outside host sandboxes and can perform typed work requests. It is loopback-only
and authenticated, but is not a multi-user isolation service. Never publish its
token or expose its port remotely.

Known API authentication overrides are blocked, but absence of an override is not
proof of subscription billing. Verify native authentication before real calls.
The alpha has no complete security audit, automatic updates or production support
commitment. See architecture.md for the precise boundaries.
