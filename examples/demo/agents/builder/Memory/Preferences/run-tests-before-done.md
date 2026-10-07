---
name: run-tests-before-done
description: The agent reported a change as finished without running the test suite
created: 2026-03-05
---

**Why:** A "done" report that skipped tests hid a failing import for a day.

**How to apply:** Run the full test suite and paste the pass/fail counts before saying a change is done.
