# ArcturionFlywheel

Turns AI-agent corrections, approvals and decisions into eval cases and a fine-tuning corpus.

Every working session with an AI agent leaves feedback behind: a correction
("no, that's not what I asked"), an approval someone granted or refused, a
decision written down so it isn't argued again. Most of it sits in notes and
logs and is never used. ArcturionFlywheel reads that material, removes
duplicates, screens it for secrets, and writes three files:

1. a **record set** with one row per piece of feedback, keyed by content hash,
2. **eval cases**, so an agent can be graded against its own past mistakes, and
3. an **SFT corpus** in chat-message format, ready for a future fine-tune.

Python 3.10+, standard library only. Read-only over its inputs.

> **Portfolio project.** This is an open-source sample of the tooling behind
> Arcturion's multi-agent setup. It is not a commercial product and makes no
> claims about revenue or customers. All bundled data is synthetic.

## Quickstart

```bash
git clone https://github.com/ArcturionTechnologies/arcturion-flywheel.git
cd arcturion-flywheel

python3 -m arcturion_flywheel \
  --agents-root examples/demo/agents \
  --ledger examples/demo/approvals-ledger.ndjson \
  --out flywheel-out
# arcturion_flywheel: {"records": 10, "eval_cases": 5, "sft_rows": 5,
#   "by_source": {"approval": 3, "feedback": 3, "decision": 2, "distill": 2}, "dropped_dupes": 0}

head -n 2 flywheel-out/evals_handoff/eval_cases.jsonl
```

The demo has four preference notes. One contains a password, so the secret
gate drops it and only three feedback records come out.

Or install it as a command: `pip install .`, then run `arcturion-flywheel ...`.

## What it reads

```
<agents-root>/
  <agent>/
    Memory/
      Preferences/*.md          corrections: description = situation, "How to apply" = right behavior
      Decisions/*.md            standing decisions (INDEX.md is skipped)
      Sessions/distilled/*.md   session summaries with "## User Requests" and "## Key Outputs"
    approval-queue.json         optional: pending/resolved approval items for that agent
<ledger>                        optional: approvals log, one JSON event per line
```

| Source | Becomes | Weight |
| --- | --- | --- |
| Preference note | `corrected` record → a `behavior` eval case + an SFT row | 1.0 |
| Denied approval | `denied` record → a `must-not` eval case | 1.0 |
| Decision note | `decided` record → an SFT row | 0.8 |
| Approved / voided approval | record only | 0.8 |
| Session summary | `trajectory` record (0.5 if it contains correction phrases like "that's not what I asked") | 0.2 / 0.5 |

Approvals are joined from the ledger: a `submitted` event supplies the gate class
and title, and the matching `resolved` event supplies the verdict. Some gates
reach the queue without a `submitted` line (for example ones raised by a hook),
so the per-agent queue file fills in the missing action text.

## What it writes

```
<out>/
  dataset/flywheel_records.jsonl    {id, ts, agent, source, category, task, agent_output,
                                     human_signal, verdict, weight, provenance}
  evals_handoff/eval_cases.jsonl    {id, agent, category, kind, prompt, expected_behavior, context, ...}
  finetune/sft_corpus.jsonl         {"messages": [system, user, assistant], "meta": {...}}
  state.json                        last run time + summary (its age is the health signal)
```

Each run rebuilds everything from scratch. For a folder of small markdown files
that is quick, and it is easier to trust than incremental state. Files
are replaced atomically, so a reader never sees a half-written file.

## The secret gate

Every text field of every record is screened before it is kept:

- **Drop the whole record** on a private-key header, a `password=`-style
  assignment, or an AWS access-key ID. A redacted credential context is still a
  credential context, so these records never enter the dataset.
- **Redact in place** key-shaped tokens (`sk-…`, Slack `xox…`, GitHub `gh…_`,
  JWTs, `api_key=…`/`token=…` assignments, chat-bot tokens). The lesson around
  the token is the useful part; the token isn't.
- **Re-check after redaction.** If anything key-shaped is still present, the
  record is dropped.

Limits, stated plainly: this is pattern matching, not a guarantee. It screens
for credentials, not for personal data such as names, emails or phone numbers.
Review a corpus before you train on it or share it.

## Scheduling

`examples/launchd/com.example.arcturion-flywheel.plist` is a macOS template for a
daily 04:15 run. Two guards make it safe to schedule:

- a **pid lock** that checks the PID really belongs to this program, so a PID
  reused after a reboot can't block runs forever, and
- an optional **gate command** in `FLYWHEEL_GATE_CMD`. If it exits `2`, the run
  is skipped (for example on thermal pressure or battery power) and the next
  scheduled run catches up.

## Configuration

Command-line flags win over environment variables.

| Flag | Environment | Default |
| --- | --- | --- |
| `--agents-root` | `FLYWHEEL_AGENTS_ROOT`, then `ARC_ROOT` | `.` |
| `--memory-glob` | `FLYWHEEL_MEMORY_GLOB` | `*/Memory` (use `*/000 */Memory` for nested homes) |
| `--ledger` | `FLYWHEEL_LEDGER` | none (approvals skipped) |
| `--queue-filename` | `FLYWHEEL_QUEUE_FILENAME` | `approval-queue.json` |
| `--out` | `FLYWHEEL_DATA_HOME` | `./flywheel-out` |
| `--lock` | `FLYWHEEL_LOCK` | `<out>/.flywheel.lock` |
| | `FLYWHEEL_GATE_CMD` | none |

The SFT system prompt and the `must-not` expected-behavior text are fields on
`arcturion_flywheel.lib.Config` if you use it as a library.

## Project layout

```
arcturion_flywheel/lib.py     harvest, curate, scrub, emit
arcturion_flywheel/cli.py     command line, pid lock, gate
examples/demo/                synthetic agents, queue and ledger
examples/launchd/             macOS schedule template
tests/test_flywheel.py        32 tests, stdlib unittest
```

## Tests

```bash
python3 -m unittest discover -s tests -v
```

Everything runs in temporary folders against synthetic data, with no network.

## License

MIT. See [LICENSE](LICENSE).

Implementation is AI-assisted; architecture, requirements, and testing directed by Robert Lingoes.
