# Give a worker enough context to finish

A useful brief describes a result, its boundaries and the evidence needed to
accept it. The worker does not automatically inherit the host conversation.
Allow it to inspect relevant target files rather than copying an entire chat.

## Example review brief

```text
Goal: Challenge the retry and partial-write behavior in the CSV import plan.
Target: /absolute/path/to/project
Start with: docs/import-plan.md, src/importer.py, tests/test_importer.py.
Scope: Read-only review. Do not edit files or publish anything.
Questions: Can a retry duplicate rows? Can a failed import partially commit?
Evidence: Cite the relevant files and give a minimal reproduction for each issue.
Completion: Distinguish blocking defects, tradeoffs and unverified assumptions.
```

For implementation, add the authorized files/behavior, known baseline, relevant
test command and acceptance criteria. State how to report an unavailable tool or
permission. Do not add more reviewers, roles or supervisors merely to run a task.

Start `fresh` when a follow-up is likely. Evaluate the result, provide the actual
changes and remaining questions, then `resume` the same provider/run ID/profile/
target. Use `fresh` for a different scope. Keep a working job's ID; a slow response
or reconnect does not justify creating a duplicate.

`wait --summary` returns a bounded answer, session, usage and path/size/hash from
the same result bytes. With `stdout_truncated: false`, collecting the answer again
adds no evidence. Still inspect the actual diff, sources and relevant tests before
acceptance. Read the referenced envelope if the answer is truncated or the summary
omits a needed field. An absent result remains an observation problem.

A worker returning `ok` is not a quality verdict. Record what the host accepted,
rejected or could not verify. Council provides optional discussion methodology;
it is not required for a single review or an implementation task.
