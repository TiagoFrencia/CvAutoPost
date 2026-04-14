"""
Prevents two pipeline instances from running simultaneously.
Chromium (~1.5 GB RAM/instance) + PostgreSQL + bot = ~2.5 GB peak.
Two concurrent runs would OOM on most home PCs.
"""
import os
from pathlib import Path
from contextlib import contextmanager

import structlog

logger = structlog.get_logger()

LOCK_FILE = Path("/tmp/auto_applier.lock")


def _is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (ProcessLookupError, PermissionError):
        return False


@contextmanager
def pipeline_lock():
    if LOCK_FILE.exists():
        pid_str = LOCK_FILE.read_text().strip()
        try:
            pid = int(pid_str)
        except ValueError:
            pid = 0
        if pid and _is_pid_alive(pid):
            raise RuntimeError(
                f"Pipeline already running (PID {pid}). "
                "If this is a stale lock, delete /tmp/auto_applier.lock and retry."
            )
        logger.warning("lock.stale_lock_removed", stale_pid=pid_str)
        LOCK_FILE.unlink(missing_ok=True)
    try:
        LOCK_FILE.write_text(str(os.getpid()))
        logger.info("lock.acquired", pid=os.getpid())
        yield
    finally:
        LOCK_FILE.unlink(missing_ok=True)
        logger.info("lock.released")
