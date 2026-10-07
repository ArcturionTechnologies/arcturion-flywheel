---
name: prefer-sqlite-for-local-state
description: Which store to use for small local tool state
created: 2026-02-20
---

Use SQLite for local tool state under 1 GB. Reach for a server database only when two
machines need to write the same data. The api_key = EXAMPLEKEYNOTREAL0000 line in an
old draft is redacted by the scrubber rather than dropping the whole decision.
