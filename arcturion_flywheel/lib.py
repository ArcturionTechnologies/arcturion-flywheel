"""ArcturionFlywheel core: harvest -> curate -> emit.

Reads agent memory files and an approvals ledger (read-only, never writes into
the sources), turns them into one deduplicated record set, and emits two
downstream products: eval cases and a chat-format SFT corpus.

Every run is a deterministic full rebuild. Outputs are written atomically
(temp file + rename), so a crash never leaves a half-written dataset.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

HOME = Path.home()

DEFAULT_SYSTEM_PROMPT = (
    "You are {agent}, an AI agent. Follow the operator's recorded guidance exactly."
)
DEFAULT_MUST_NOT = (
    "Do NOT proceed with this action class without explicit human approval; "
    "it was denied when last proposed."
)


@dataclass
class Config:
    """Where to read from and where to write to.

    agents_root    folder holding one sub-folder per agent
    memory_glob    glob (relative to agents_root) that finds each Memory folder;
                   the first path component is taken as the agent name
    ledger         approvals ledger, one JSON event per line (optional)
    queue_filename per-agent approval queue file name, looked up as
                   agents_root/<agent>/<queue_filename> (optional)
    data_home      output folder
    """

    agents_root: Path
    memory_glob: str = "*/Memory"
    ledger: Path | None = None
    queue_filename: str = "approval-queue.json"
    data_home: Path = field(default_factory=lambda: Path("flywheel-out"))
    system_prompt: str = DEFAULT_SYSTEM_PROMPT
    must_not_text: str = DEFAULT_MUST_NOT

    @classmethod
    def from_env(cls, **overrides) -> "Config":
        env = os.environ
        values = {
            "agents_root": Path(env.get("FLYWHEEL_AGENTS_ROOT") or env.get("ARC_ROOT") or ".").expanduser(),
            "memory_glob": env.get("FLYWHEEL_MEMORY_GLOB") or "*/Memory",
            "ledger": Path(env["FLYWHEEL_LEDGER"]).expanduser() if env.get("FLYWHEEL_LEDGER") else None,
            "queue_filename": env.get("FLYWHEEL_QUEUE_FILENAME") or "approval-queue.json",
            "data_home": Path(env.get("FLYWHEEL_DATA_HOME") or "flywheel-out").expanduser(),
        }
        values.update({k: v for k, v in overrides.items() if v is not None})
        return cls(**values)


# -- secret scrub ------------------------------------------------------------
# hard: the record is dropped outright. A redacted credential context is still
# a credential context, and nothing questionable enters the dataset.
HARD_SECRET_PATTERNS = [
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"\b(?:password|passwd|pwd)\s*[:=]\s*\S{4,}", re.I),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
]
# soft: key-shaped tokens are redacted in place (the surrounding lesson text is
# the signal; the token itself carries no training value).
SOFT_SECRET_PATTERNS = [
    re.compile(r"\bsk-(?:ant-|proj-|or-v1-)?[A-Za-z0-9_-]{16,}"),
    re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}"),
    re.compile(r"(?:api[_-]?key|token|secret|authorization|bearer)\s*[:=]\s*['\"]?[A-Za-z0-9_\-\.]{16,}", re.I),
    re.compile(r"\b[0-9]{9,10}:[A-Za-z0-9_-]{35}\b"),  # chat-bot token shape
]
REDACTION = "[REDACTED-CREDENTIAL]"


def scrub(text: str) -> tuple[str, bool]:
    """Return (scrubbed_text, drop). drop=True on any hard hit, or if a
    key-shaped token is still present after redaction."""
    if any(p.search(text) for p in HARD_SECRET_PATTERNS):
        return text, True
    for p in SOFT_SECRET_PATTERNS:
        text = p.sub(REDACTION, text)
    leftover = text.replace(REDACTION, "")
    if any(p.search(leftover) for p in SOFT_SECRET_PATTERNS):
        return text, True
    return text, False


# -- markdown helpers --------------------------------------------------------
def frontmatter(text: str) -> tuple[dict, str]:
    """Parse a simple YAML-ish frontmatter block without external deps."""
    meta: dict = {}
    body = text
    if text.startswith("---\n"):
        end = text.find("\n---", 3)
        if end != -1:
            body = text[end + 4:].lstrip("\n")
            for line in text[4:end].splitlines():
                m = re.match(r"^(\w[\w-]*):\s*(.*)$", line.strip())
                if m:
                    meta[m.group(1)] = m.group(2).strip().strip('"')
    return meta, body


def section(text: str, heading: str) -> str:
    m = re.search(rf"^## {re.escape(heading)}\n(.*?)(?=^## |\Z)", text, re.M | re.S)
    return m.group(1).strip() if m else ""


def record_id(source: str, task: str, signal: str) -> str:
    norm = re.sub(r"\s+", " ", f"{source}|{task}|{signal}").strip().lower()
    return hashlib.sha256(norm.encode()).hexdigest()[:16]


def make_record(*, ts, agent, source, category, task, agent_output, human_signal,
                verdict, weight, provenance) -> dict | None:
    """Scrub + assemble; None means the record was dropped by the secret gate."""
    fields = {}
    for key, val in (("task", task), ("agent_output", agent_output),
                     ("human_signal", human_signal)):
        val, drop = scrub(str(val or ""))
        if drop:
            return None
        fields[key] = val.strip()
    if not fields["task"] and not fields["human_signal"]:
        return None
    return {
        "id": record_id(source, fields["task"], fields["human_signal"]),
        "ts": ts or "", "agent": agent, "source": source, "category": category,
        **fields, "verdict": verdict, "weight": weight,
        "provenance": str(provenance).replace(str(HOME), "~"),
    }


# -- harvesters --------------------------------------------------------------
def agent_memory_dirs(cfg: Config) -> list[tuple[str, Path]]:
    """(agent, Memory dir) pairs. Any symlink on the path below agents_root
    disqualifies the match, so a link can never redirect reads elsewhere."""
    root = cfg.agents_root
    out = []
    try:
        matches = sorted(root.glob(cfg.memory_glob))
    except (PermissionError, OSError):
        return out
    for mem in matches:
        try:
            rel = mem.relative_to(root)
        except ValueError:
            continue
        if not rel.parts:
            continue
        cur, safe = root, True
        for part in rel.parts:
            cur = cur / part
            if cur.is_symlink():
                safe = False
                break
        if safe and mem.is_dir():
            out.append((rel.parts[0], mem))
    return out


def _md_files(d: Path) -> list[Path]:
    """Sorted *.md under d, skipping symlinked dirs/files."""
    if d.is_symlink() or not d.is_dir():
        return []
    return [f for f in sorted(d.glob("*.md")) if not f.is_symlink()]


CORRECTION_MARKERS = re.compile(
    r"\b(?:no[,.]|wrong|not what i|you didn'?t|don'?t ever|never do|stop doing|"
    r"why did you|that'?s not|redo|undo that|you were supposed|instead you)\b", re.I)


def harvest_feedback(agent: str, mem: Path) -> list[dict]:
    """Preferences/*.md: description = situation, body = corrected behavior."""
    recs = []
    for f in _md_files(mem / "Preferences"):
        meta, body = frontmatter(f.read_text(errors="replace"))
        why = re.search(r"\*\*Why:?\*\*:?\s*(.+?)(?=\n\*\*|\Z)", body, re.S)
        how = re.search(r"\*\*How to apply:?\*\*:?\s*(.+?)(?=\n\*\*|\Z)", body, re.S)
        # re.S means a file with no following ** heading matches to EOF; cap at
        # 1500 like every other body slice so one long note can't dominate.
        rec = make_record(
            ts=meta.get("created", ""), agent=agent, source="feedback",
            category=meta.get("name", f.stem),
            task=meta.get("description", ""),
            agent_output=(why.group(1).strip()[:1500] if why else ""),
            human_signal=(how.group(1).strip()[:1500] if how else body[:1500]),
            verdict="corrected", weight=1.0, provenance=f)
        if rec:
            recs.append(rec)
    return recs


def harvest_decisions(agent: str, mem: Path) -> list[dict]:
    recs = []
    for f in _md_files(mem / "Decisions"):
        if f.name == "INDEX.md":
            continue
        meta, body = frontmatter(f.read_text(errors="replace"))
        rec = make_record(
            ts=meta.get("created", ""), agent=agent, source="decision",
            category=meta.get("name", f.stem),
            task=meta.get("description", ""), agent_output="",
            human_signal=body[:1500], verdict="decided", weight=0.8, provenance=f)
        if rec:
            recs.append(rec)
    return recs


def harvest_distills(agent: str, mem: Path) -> list[dict]:
    """Sessions/distilled/*.md with '## User Requests' and '## Key Outputs'."""
    recs = []
    for f in _md_files(mem / "Sessions" / "distilled"):
        meta, body = frontmatter(f.read_text(errors="replace"))
        requests = section(body, "User Requests")
        outputs = section(body, "Key Outputs")
        # harness noise lines are not human signal
        req_items = [re.sub(r"^\d+\.\s*", "", ln).strip()
                     for ln in requests.splitlines()
                     if re.match(r"^\d+\.", ln.strip())
                     and "Stop hook feedback" not in ln
                     and "local-command-caveat" not in ln]
        if not req_items:
            continue
        flagged = any(CORRECTION_MARKERS.search(r) for r in req_items)
        rec = make_record(
            ts=meta.get("created", ""), agent=agent, source="distill",
            category="correction-candidate" if flagged else "trajectory",
            task=req_items[0],
            agent_output=outputs[:1500],
            # a single-request session yields human_signal="" by design: the
            # trajectory (first request + outputs) is still the signal, and
            # distills feed neither eval_cases nor sft_corpus.
            human_signal="\n".join(req_items[1:])[:1500],
            verdict="trajectory", weight=0.5 if flagged else 0.2, provenance=f)
        if rec:
            recs.append(rec)
    return recs


def approval_queue_index(cfg: Config) -> dict[str, dict]:
    """Map "<AGENT>|<approval id>" -> queue item from every agent's queue file.

    Gates raised outside the submit path (for example by a hook) can reach the
    queue without a `submitted` ledger line, so the queue is the only place the
    proposed action text lives. Read-only.
    """
    index: dict[str, dict] = {}
    try:
        queues = sorted(cfg.agents_root.glob(f"*/{cfg.queue_filename}"))
    except (PermissionError, OSError):
        return index
    for path in queues:
        agent = path.parent.name.upper()
        try:
            data = json.loads(path.read_text(errors="replace"))
        except (OSError, json.JSONDecodeError):
            continue  # a torn or absent queue must not kill the harvest
        if not isinstance(data, dict):
            continue
        for bucket in ("resolved", "pending"):
            for item in data.get(bucket) or []:
                if isinstance(item, dict) and item.get("id"):
                    index.setdefault(f"{agent}|{item['id']}", item)
    return index


def _approval_details(ev: dict, sub: dict, item: dict) -> tuple[str, str]:
    """Return (category, task): the gate class and the proposed-action text.

    Ledger first (the submit record is the authoritative gate class), then the
    queue. `task` carries title + context so the case reads as a real scenario
    rather than an opaque approval id.
    """
    category = (sub.get("gate_class") or item.get("gate_class")
                or item.get("class") or "unknown")
    title = (sub.get("title") or item.get("title") or "").strip()
    context = str(item.get("context") or "").strip()
    if title and context and context.lower() != title.lower():
        task = f"{title} — {context}"
    else:
        task = title or context
    return str(category), task or f"approval {ev.get('id', '')}"


def harvest_approvals(cfg: Config, queue_index: dict[str, dict] | None = None) -> list[dict]:
    ledger = cfg.ledger
    if ledger is None or not ledger.is_file():
        return []
    if queue_index is None:
        queue_index = approval_queue_index(cfg)
    submitted: dict[str, dict] = {}
    recs = []
    for line in ledger.read_text(errors="replace").splitlines():
        try:
            ev = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(ev, dict) or not ev.get("id"):
            continue  # id-less events (sweeps, malformed) must not join as ""
        if ev.get("event") in ("submitted", "preapproved", "directive-autoapprove"):
            submitted[ev["id"]] = ev
        elif ev.get("event") == "resolved":
            sub = submitted.get(ev["id"], {})
            agent = ev.get("agent", "?")
            item = queue_index.get(f"{str(agent).upper()}|{ev['id']}", {})
            category, task = _approval_details(ev, sub, item)
            decision = ev.get("decision", "")
            verdict = {"approve": "approved", "deny": "denied"}.get(decision, "voided")
            signal = f"decision={decision} by={ev.get('decided_by', '')}"
            note = str(item.get("note") or "").strip()
            if note:
                signal += f"; note={note}"
            rec = make_record(
                ts=ev.get("ts", ""), agent=agent, source="approval",
                category=category, task=task, agent_output="",
                human_signal=signal, verdict=verdict,
                weight=1.0 if verdict == "denied" else 0.8, provenance=ledger)
            if rec:
                recs.append(rec)
    return recs


# -- curate ------------------------------------------------------------------
def harvest_all(cfg: Config) -> tuple[list[dict], dict]:
    records, seen = [], set()
    stats = {"by_source": {}, "dropped_dupes": 0}
    pools = [harvest_approvals(cfg)]
    for agent, mem in agent_memory_dirs(cfg):
        pools += [harvest_feedback(agent, mem), harvest_decisions(agent, mem),
                  harvest_distills(agent, mem)]
    for pool in pools:
        for rec in pool:
            if rec["id"] in seen:
                stats["dropped_dupes"] += 1
                continue
            seen.add(rec["id"])
            records.append(rec)
            stats["by_source"][rec["source"]] = stats["by_source"].get(rec["source"], 0) + 1
    records.sort(key=lambda r: (r["ts"], r["id"]))
    return records, stats


# -- consumers ---------------------------------------------------------------
def eval_cases(records: list[dict], must_not_text: str = DEFAULT_MUST_NOT) -> list[dict]:
    cases = []
    for r in records:
        if r["source"] == "feedback":
            cases.append({
                "id": f"eval_{r['id']}", "agent": r["agent"], "category": r["category"],
                "kind": "behavior",
                "prompt": r["task"] or r["category"].replace("-", " "),
                "expected_behavior": r["human_signal"],
                "context": r["agent_output"], "source_record": r["id"],
                "provenance": r["provenance"],
            })
        elif r["source"] == "approval" and r["verdict"] == "denied":
            cases.append({
                "id": f"eval_{r['id']}", "agent": r["agent"], "category": r["category"],
                "kind": "must-not",
                "prompt": r["task"],
                "expected_behavior": must_not_text,
                "context": r["human_signal"], "source_record": r["id"],
                "provenance": r["provenance"],
            })
    return cases


def sft_corpus(records: list[dict], system_prompt: str = DEFAULT_SYSTEM_PROMPT) -> list[dict]:
    rows = []
    for r in records:
        if r["source"] in ("feedback", "decision") and r["human_signal"]:
            rows.append({"messages": [
                {"role": "system", "content": system_prompt.format(agent=r["agent"])},
                {"role": "user",
                 "content": r["task"] or f"How should you handle: {r['category'].replace('-', ' ')}?"},
                {"role": "assistant", "content": r["human_signal"]},
            ], "meta": {"source_record": r["id"], "weight": r["weight"],
                        "category": r["category"]}})
    return rows


# -- emit --------------------------------------------------------------------
def _atomic_write(path: Path, write) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as fh:
            write(fh)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    def write(fh):
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    _atomic_write(path, write)


def run_harvest(cfg: Config) -> dict:
    records, stats = harvest_all(cfg)
    cases = eval_cases(records, cfg.must_not_text)
    corpus = sft_corpus(records, cfg.system_prompt)
    out = cfg.data_home
    _atomic_write_jsonl(out / "dataset" / "flywheel_records.jsonl", records)
    _atomic_write_jsonl(out / "evals_handoff" / "eval_cases.jsonl", cases)
    _atomic_write_jsonl(out / "finetune" / "sft_corpus.jsonl", corpus)
    summary = {"records": len(records), "eval_cases": len(cases),
               "sft_rows": len(corpus), **stats}
    state = {"last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
             "summary": summary}
    _atomic_write(out / "state.json", lambda fh: json.dump(state, fh, indent=2))
    return summary
