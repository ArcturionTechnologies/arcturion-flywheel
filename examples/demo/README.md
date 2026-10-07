# Demo data (synthetic)

Everything in this folder is invented for the example. No real agent memory,
approvals or people are represented.

- `agents/builder/` and `agents/researcher/`: two fake agents with a few
  preference, decision and session-summary files each
- `agents/builder/approval-queue.json`: the builder agent's approval queue
- `approvals-ledger.ndjson`: one JSON event per line, the shared approvals log

One preference file deliberately contains a password assignment, so you can
watch the secret gate drop that record.
