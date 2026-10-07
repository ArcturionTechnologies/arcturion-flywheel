#!/usr/bin/env python3
"""ArcturionFlywheel tests: parsers, secret gate, dedup, consumers, entry guards.

Everything runs in temporary folders against synthetic data. No network.
"""
import json
import sys
import tempfile
import unittest
import unittest.mock
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))
from arcturion_flywheel import cli as entry  # noqa: E402
from arcturion_flywheel import lib  # noqa: E402

# Secret-shaped test strings are assembled at runtime so the repository itself
# never contains anything a secret scanner would flag.
FAKE_ANTHROPIC = "sk-" + "ant-" + "abc123def456ghi789jkl"
FAKE_BOT_TOKEN = "1234567890" + ":" + "AAH" + "x" * 32
FAKE_PEM = "-----BEGIN " + "RSA PRIVATE KEY-----"

FEEDBACK_MD = """---
name: restart-before-workaround
description: Try a restart before building a workaround
metadata:
  type: feedback
---

Body text.

**Why:** The operator called this out after wasted research.

**How to apply:** Restart the app and re-check before designing a bigger fix.
"""

DISTILL_MD = """---
title: "Session 2026-07-04 [claude] — fix the bridge"
owner: builder
created: 2026-07-04
---

## User Requests
1. Fix the bridge timeout bug.
2. Stop hook feedback: knowledge capture nag.
3. no, that's not what I asked — you didn't fix the retry path.

## Key Outputs
- Patched retry path.

## Stats
- whatever
"""


def cfg_for(tmp, **kw):
    return lib.Config(agents_root=Path(tmp), data_home=Path(tmp) / "out", **kw)


class Scrub(unittest.TestCase):
    def test_hard_hit_drops(self):
        _, drop = lib.scrub("the password= hunter2secret was set")
        self.assertTrue(drop)
        self.assertTrue(lib.scrub(FAKE_PEM)[1])

    def test_soft_hit_redacts(self):
        text, drop = lib.scrub(f"use {FAKE_ANTHROPIC} for calls")
        self.assertFalse(drop)
        self.assertIn("[REDACTED-CREDENTIAL]", text)
        self.assertNotIn("sk-ant", text)

    def test_bot_token_redacted(self):
        text, _ = lib.scrub(f"token {FAKE_BOT_TOKEN}")
        self.assertIn("[REDACTED-CREDENTIAL]", text)

    def test_clean_text_untouched(self):
        text, drop = lib.scrub("restart the app before workarounds")
        self.assertFalse(drop)
        self.assertNotIn("REDACTED", text)

    def test_record_with_hard_secret_is_dropped(self):
        rec = lib.make_record(ts="", agent="A", source="feedback", category="c",
                              task="password= hunter2secret", agent_output="",
                              human_signal="x", verdict="corrected",
                              weight=1.0, provenance="p")
        self.assertIsNone(rec)

    def test_leftover_key_shape_after_redaction_drops_record(self):
        # Simulate a redaction pass that misses something: the post-check must
        # still refuse the record rather than let a key-shaped token through.
        class NoRedact:
            def __init__(self, pattern):
                self.pattern = pattern

            def sub(self, repl, text):
                return text  # a broken redactor

            def search(self, text):
                return self.pattern.search(text)

        broken = [NoRedact(lib.SOFT_SECRET_PATTERNS[0])]
        with unittest.mock.patch.object(lib, "SOFT_SECRET_PATTERNS", broken):
            _, drop = lib.scrub(f"key {FAKE_ANTHROPIC}")
        self.assertTrue(drop)


class Parsers(unittest.TestCase):
    def _tree(self, tmp):
        mem = Path(tmp) / "Memory"
        (mem / "Preferences").mkdir(parents=True)
        (mem / "Decisions").mkdir()
        (mem / "Sessions" / "distilled").mkdir(parents=True)
        (mem / "Preferences" / "feedback_x.md").write_text(FEEDBACK_MD)
        (mem / "Decisions" / "some-ruling.md").write_text(
            "---\nname: some-ruling\ndescription: A ruling\ncreated: 2026-07-01\n---\nNever do X; do Y.\n")
        (mem / "Decisions" / "INDEX.md").write_text("index — must be skipped")
        (mem / "Sessions" / "distilled" / "claude-abc.md").write_text(DISTILL_MD)
        return mem

    def test_feedback_parse(self):
        with tempfile.TemporaryDirectory() as tmp:
            recs = lib.harvest_feedback("builder", self._tree(tmp))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["verdict"], "corrected")
        self.assertEqual(r["category"], "restart-before-workaround")
        self.assertIn("Restart the app", r["human_signal"])
        self.assertEqual(r["weight"], 1.0)

    def test_frontmatter_keeps_first_key_after_delimiter(self):
        meta, body = lib.frontmatter("---\nname: first-key\nowner: builder\n---\nBody.\n")
        self.assertEqual(meta["name"], "first-key")
        self.assertEqual(meta["owner"], "builder")
        self.assertEqual(body, "Body.\n")

    def test_feedback_unbounded_why_how_are_capped(self):
        long_md = ("---\nname: long-one\ndescription: d\n---\n\n"
                   "**Why:** " + "w" * 5000 + "\n\n**How to apply:** " + "h" * 5000 + "\n")
        with tempfile.TemporaryDirectory() as tmp:
            mem = Path(tmp) / "Memory"
            (mem / "Preferences").mkdir(parents=True)
            (mem / "Preferences" / "long.md").write_text(long_md)
            recs = lib.harvest_feedback("builder", mem)
        self.assertEqual(len(recs), 1)
        self.assertEqual(len(recs[0]["agent_output"]), 1500)
        self.assertEqual(len(recs[0]["human_signal"]), 1500)

    def test_symlinked_memory_files_and_dirs_are_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            outside = Path(tmp) / "outside"
            outside.mkdir()
            (outside / "leaked.md").write_text(FEEDBACK_MD)
            mem = Path(tmp) / "Memory"
            (mem / "Preferences").mkdir(parents=True)
            (mem / "Preferences" / "link.md").symlink_to(outside / "leaked.md")
            (mem / "Decisions").symlink_to(outside, target_is_directory=True)
            self.assertEqual(lib.harvest_feedback("builder", mem), [])
            self.assertEqual(lib.harvest_decisions("builder", mem), [])

    def test_symlinked_agent_dir_is_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "agents"
            (root / "real" / "Memory").mkdir(parents=True)
            elsewhere = Path(tmp) / "elsewhere"
            (elsewhere / "Memory").mkdir(parents=True)
            (root / "linked").symlink_to(elsewhere, target_is_directory=True)
            found = lib.agent_memory_dirs(lib.Config(agents_root=root))
        self.assertEqual([a for a, _ in found], ["real"])

    def test_memory_glob_supports_nested_layouts(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "builder" / "000 home" / "Memory").mkdir(parents=True)
            found = lib.agent_memory_dirs(lib.Config(agents_root=root, memory_glob="*/000 */Memory"))
        self.assertEqual([a for a, _ in found], ["builder"])

    def test_decisions_parse_skips_index(self):
        with tempfile.TemporaryDirectory() as tmp:
            recs = lib.harvest_decisions("builder", self._tree(tmp))
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "decided")

    def test_distill_parse_flags_correction_and_strips_noise(self):
        with tempfile.TemporaryDirectory() as tmp:
            recs = lib.harvest_distills("builder", self._tree(tmp))
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["task"], "Fix the bridge timeout bug.")
        self.assertNotIn("Stop hook feedback", r["human_signal"])
        self.assertEqual(r["category"], "correction-candidate")
        self.assertEqual(r["weight"], 0.5)

    def _harvest(self, ledger, queue_index):
        with tempfile.TemporaryDirectory() as tmp:
            lf = Path(tmp) / "ledger.ndjson"
            lf.write_text("\n".join(json.dumps(e) for e in ledger))
            return lib.harvest_approvals(cfg_for(tmp, ledger=lf), queue_index=queue_index)

    def test_approvals_join(self):
        ledger = [
            {"ts": "t1", "event": "submitted", "agent": "helper", "id": "a1",
             "gate_class": "outbound-send", "title": "Send email to client"},
            {"ts": "t2", "event": "resolved", "agent": "helper", "id": "a1",
             "decision": "deny", "decided_by": "operator"},
            {"ts": "t3", "event": "sweep", "summary": {}},
        ]
        recs = self._harvest(ledger, {})
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["verdict"], "denied")
        self.assertEqual(recs[0]["weight"], 1.0)
        self.assertEqual(recs[0]["task"], "Send email to client")

    def test_no_ledger_configured_yields_nothing(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(lib.harvest_approvals(cfg_for(tmp)), [])

    def test_approvals_join_falls_back_to_queue_for_hook_raised_gates(self):
        # hook-raised gates never emit a `submitted` line; the action text is
        # only in the agent's approval queue file.
        ledger = [{"ts": "t2", "event": "resolved", "agent": "builder", "id": "a9",
                   "decision": "deny", "decided_by": "operator-inline"}]
        queue = {"BUILDER|a9": {"id": "a9", "title": "Stuck in a repeating pattern on Bash",
                                "context": "I've run 6 similar-looking Bash commands in 5 min.",
                                "class": "pattern", "note": "Operator declined via digest"}}
        recs = self._harvest(ledger, queue)
        self.assertEqual(len(recs), 1)
        r = recs[0]
        self.assertEqual(r["category"], "pattern")
        self.assertEqual(r["task"], "Stuck in a repeating pattern on Bash — "
                                    "I've run 6 similar-looking Bash commands in 5 min.")
        self.assertGreaterEqual(len(r["task"]), 60)
        self.assertIn("note=Operator declined via digest", r["human_signal"])

    def test_approvals_join_prefers_ledger_submit_over_queue(self):
        ledger = [
            {"ts": "t1", "event": "submitted", "agent": "helper", "id": "a1",
             "gate_class": "outbound-send", "title": "Send email to client"},
            {"ts": "t2", "event": "resolved", "agent": "helper", "id": "a1",
             "decision": "deny", "decided_by": "operator"},
        ]
        queue = {"HELPER|a1": {"id": "a1", "title": "stale title", "class": "other",
                               "context": "recipient=client@example.com"}}
        r = self._harvest(ledger, queue)[0]
        self.assertEqual(r["category"], "outbound-send")
        self.assertEqual(r["task"], "Send email to client — recipient=client@example.com")

    def test_approvals_unjoined_id_still_degrades_to_stub(self):
        ledger = [{"ts": "t", "event": "resolved", "agent": "researcher", "id": "zz",
                   "decision": "deny", "decided_by": "operator"}]
        r = self._harvest(ledger, {})[0]
        self.assertEqual(r["task"], "approval zz")
        self.assertEqual(r["category"], "unknown")

    def test_queue_index_skips_torn_queue_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "builder").mkdir()
            (root / "builder" / "approval-queue.json").write_text(
                json.dumps({"pending": [{"id": "p1", "title": "T"}], "resolved": []}))
            (root / "broken").mkdir()
            (root / "broken" / "approval-queue.json").write_text("{not json")
            idx = lib.approval_queue_index(cfg_for(tmp))
        self.assertEqual(list(idx), ["BUILDER|p1"])


class Consumers(unittest.TestCase):
    def _feedback_rec(self):
        return lib.make_record(ts="2026-07-01", agent="builder", source="feedback",
                               category="cat", task="situation",
                               agent_output="why", human_signal="do it right",
                               verdict="corrected", weight=1.0, provenance="p")

    def test_eval_case_from_feedback(self):
        cases = lib.eval_cases([self._feedback_rec()])
        self.assertEqual(len(cases), 1)
        self.assertEqual(cases[0]["kind"], "behavior")
        self.assertEqual(cases[0]["expected_behavior"], "do it right")

    def test_eval_case_from_denial_only(self):
        approved = lib.make_record(ts="", agent="A", source="approval", category="g",
                                   task="t", agent_output="", human_signal="s",
                                   verdict="approved", weight=0.8, provenance="p")
        denied = lib.make_record(ts="", agent="A", source="approval", category="g",
                                 task="t2", agent_output="", human_signal="s",
                                 verdict="denied", weight=1.0, provenance="p")
        cases = lib.eval_cases([approved, denied])
        self.assertEqual([c["kind"] for c in cases], ["must-not"])

    def test_sft_rows_chat_format(self):
        rows = lib.sft_corpus([self._feedback_rec()])
        self.assertEqual(len(rows), 1)
        roles = [m["role"] for m in rows[0]["messages"]]
        self.assertEqual(roles, ["system", "user", "assistant"])
        self.assertEqual(rows[0]["messages"][2]["content"], "do it right")
        self.assertIn("builder", rows[0]["messages"][0]["content"])

    def test_sft_system_prompt_is_configurable(self):
        rows = lib.sft_corpus([self._feedback_rec()], system_prompt="Agent {agent}.")
        self.assertEqual(rows[0]["messages"][0]["content"], "Agent builder.")

    def test_dedup(self):
        a, b = self._feedback_rec(), self._feedback_rec()
        self.assertEqual(a["id"], b["id"])


class EmitAndEntry(unittest.TestCase):
    def test_atomic_write_and_no_tmp_left(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "out" / "x.jsonl"
            lib._atomic_write_jsonl(p, [{"a": 1}, {"b": 2}])
            self.assertEqual(len(p.read_text().splitlines()), 2)
            self.assertEqual([f for f in p.parent.iterdir() if f.suffix == ".tmp"], [])

    def test_state_write_failure_leaves_no_orphan_tmp(self):
        real_replace = lib.os.replace

        def boom(src, dst):
            if str(dst).endswith("state.json"):
                raise OSError("disk full")
            return real_replace(src, dst)

        with tempfile.TemporaryDirectory() as tmp:
            cfg = cfg_for(tmp)
            with unittest.mock.patch.object(lib.os, "replace", boom), \
                    unittest.mock.patch.object(lib, "harvest_all", lambda c: ([], {})):
                with self.assertRaises(OSError):
                    lib.run_harvest(cfg)
            self.assertEqual([f for f in cfg.data_home.iterdir() if f.suffix == ".tmp"], [])

    def test_lock_cycle_is_idempotent(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "locks" / "fw.lock"
            entry.release_lock(lock)
            self.assertTrue(entry.acquire_lock(lock))
            with unittest.mock.patch.object(entry, "_lock_pid_is_ours", lambda pid: True):
                self.assertFalse(entry.acquire_lock(lock))  # a live owner holds it
            entry.release_lock(lock)
            self.assertTrue(entry.acquire_lock(lock))
            entry.release_lock(lock)

    def test_stale_lock_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as tmp:
            lock = Path(tmp) / "fw.lock"
            lock.write_text("not-a-pid")
            self.assertTrue(entry.acquire_lock(lock))
            entry.release_lock(lock)

    def test_gate_without_command_is_ok(self):
        self.assertTrue(entry.gate_ok(""))

    def test_gate_exit_2_skips(self):
        self.assertFalse(entry.gate_ok(f"{sys.executable} -c 'raise SystemExit(2)'"))
        self.assertTrue(entry.gate_ok(f"{sys.executable} -c 'raise SystemExit(0)'"))


class BundledExample(unittest.TestCase):
    """End-to-end over examples/demo, which is entirely synthetic."""

    def test_cli_over_demo_data(self):
        demo = REPO / "examples" / "demo"
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "out"
            with unittest.mock.patch.dict(entry.os.environ, {}, clear=False):
                rc = entry.main(["--agents-root", str(demo / "agents"),
                                 "--ledger", str(demo / "approvals-ledger.ndjson"),
                                 "--out", str(out)])
            self.assertEqual(rc, 0)
            records = [json.loads(x) for x in (out / "dataset" / "flywheel_records.jsonl").read_text().splitlines()]
            cases = (out / "evals_handoff" / "eval_cases.jsonl").read_text().splitlines()
            sft = (out / "finetune" / "sft_corpus.jsonl").read_text().splitlines()
            state = json.loads((out / "state.json").read_text())
            self.assertFalse((out / ".flywheel.lock").exists())

        self.assertEqual(len(records), 10)
        self.assertEqual(len(cases), 5)
        self.assertEqual(len(sft), 5)
        self.assertEqual(state["summary"]["by_source"],
                         {"approval": 3, "feedback": 3, "decision": 2, "distill": 2})
        blob = json.dumps(records)
        # the password-bearing preference was dropped, the key line was redacted
        self.assertNotIn("correct-horse-example", blob)
        self.assertNotIn("EXAMPLEKEYNOTREAL0000", blob)
        self.assertIn("[REDACTED-CREDENTIAL]", blob)
        # the hook-raised gate picked up its text from the queue file
        self.assertTrue(any("Repeated near-identical shell commands" in r["task"] for r in records))


if __name__ == "__main__":
    unittest.main(verbosity=2)
