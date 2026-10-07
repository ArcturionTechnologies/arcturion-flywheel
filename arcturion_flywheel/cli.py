"""ArcturionFlywheel command line: one batch harvest, safe to run on a schedule.

Guards:
  * pid lock with an identity check, so a reused PID after a reboot can't wedge it
  * optional pre-run gate command (FLYWHEEL_GATE_CMD); exit code 2 means
    "skip this run" (for example a thermal or battery check)
The health signal for a batch job is the freshness of <data_home>/state.json.
"""
from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from . import lib

NAME = "arcturion_flywheel"


def gate_ok(cmd: str | None = None) -> bool:
    """True unless the gate command exits 2. No gate configured -> True."""
    cmd = cmd if cmd is not None else os.environ.get("FLYWHEEL_GATE_CMD", "")
    if not cmd:
        return True
    try:
        return subprocess.run(shlex.split(cmd), capture_output=True, timeout=15).returncode != 2
    except Exception:
        return True


def _lock_pid_is_ours(pid: int) -> bool:
    # PID liveness alone is unsafe: after a reboot the OS reuses PIDs. The lock
    # counts only if the live PID is actually running this program.
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, PermissionError):
        return False
    try:
        out = subprocess.run(["ps", "-p", str(pid), "-o", "command="],
                             capture_output=True, text=True, timeout=5).stdout
    except Exception:
        return False
    return "arcturion_flywheel" in out or "arcturion-flywheel" in out


def _try_create_lock(lock: Path) -> bool:
    # O_CREAT|O_EXCL is the atomic claim: no exists()->remove()->write() race
    try:
        fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w") as fh:
        fh.write(str(os.getpid()))
    return True


def acquire_lock(lock: Path) -> bool:
    lock.parent.mkdir(parents=True, exist_ok=True)
    if _try_create_lock(lock):
        return True
    try:
        pid = int(lock.read_text().strip())
    except (ValueError, OSError):
        pid = -1
    if pid > 0 and _lock_pid_is_ours(pid):
        return False  # already running
    try:
        lock.unlink()  # stale: dead, reused by another process, or garbage
    except OSError:
        pass
    return _try_create_lock(lock)


def release_lock(lock: Path) -> None:
    try:
        lock.unlink()
    except OSError:
        pass


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="arcturion-flywheel", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--agents-root", type=Path,
                    help="folder with one sub-folder per agent (env FLYWHEEL_AGENTS_ROOT, then ARC_ROOT)")
    ap.add_argument("--memory-glob", help="glob under agents-root that finds Memory folders (default */Memory)")
    ap.add_argument("--ledger", type=Path, help="approvals ledger, one JSON event per line (env FLYWHEEL_LEDGER)")
    ap.add_argument("--queue-filename", help="per-agent approval queue file name (default approval-queue.json)")
    ap.add_argument("--out", type=Path, dest="data_home", help="output folder (env FLYWHEEL_DATA_HOME, default ./flywheel-out)")
    ap.add_argument("--lock", type=Path, help="lock file (env FLYWHEEL_LOCK, default <out>/.flywheel.lock)")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    cfg = lib.Config.from_env(agents_root=args.agents_root, memory_glob=args.memory_glob,
                              ledger=args.ledger, queue_filename=args.queue_filename,
                              data_home=args.data_home)
    lock = args.lock or Path(os.environ.get("FLYWHEEL_LOCK") or cfg.data_home / ".flywheel.lock")
    if not gate_ok():
        print(f"{NAME}: gate command said skip; the next scheduled run catches up")
        return 0
    if not acquire_lock(lock):
        print(f"{NAME} already running", file=sys.stderr)
        return 0
    try:
        summary = lib.run_harvest(cfg)
        print(f"{NAME}: {json.dumps(summary, ensure_ascii=False)}")
        return 0
    finally:
        release_lock(lock)


if __name__ == "__main__":
    raise SystemExit(main())
