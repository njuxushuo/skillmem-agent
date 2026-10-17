"""B1 execution limits and same-conversation exclusion; no business or agent planning logic."""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from time import time


@dataclass(frozen=True)
class ExecutionLimits:
    model_calls: int = 8
    tool_calls: int = 8  # includes actual resume attempts, not just unique proposals
    turn_seconds: float = 120
    tool_seconds: float = 5

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in (self.model_calls, self.tool_calls)):
            raise ValueError("Call limits must be positive integers")
        if any(not 0 < v <= 3600 for v in (self.turn_seconds, self.tool_seconds)):
            raise ValueError("Timeouts must be finite, positive and at most 3600 seconds")


class ExecutionStopped(RuntimeError):
    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


def check_deadline(deadline: float | None):
    if deadline is not None and time() >= deadline:
        raise TimeoutError("execution_deadline_exceeded")


def tool_error(code: str, *, retryable: bool = False) -> str:
    return json.dumps({"error": code, "status": "error", "retryable": retryable})


@contextmanager
def database_lock(db_path: str | None, identity: str):
    """Local-filesystem POSIX lock, shared by service instances and released on process death."""
    if not db_path:
        yield
        return
    directory = Path(str(Path(db_path).resolve()) + ".locks")
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    name = hashlib.sha256(identity.encode()).hexdigest()
    with os.fdopen(os.open(directory / name, os.O_CREAT | os.O_RDWR, 0o600), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


class ExecutionControl:
    """Durable attempt counts; restart/resume never resets an unfinished turn's budget.

    File locks are released by the OS on process death. A service-local RLock also protects
    its single checkpointer connection; different services/processes share the file lock.
    """

    def __init__(self, db_path: str | None, limits: ExecutionLimits):
        self.limits = limits
        self._db = sqlite3.connect(db_path or ":memory:", check_same_thread=False, timeout=5)
        self._lock = threading.RLock()
        self._session_lock = threading.RLock()
        self.db_path = db_path
        with self._db:
            self._db.execute(
                "CREATE TABLE IF NOT EXISTS execution_turns "
                "(thread_id TEXT PRIMARY KEY, model_calls INTEGER, tool_calls INTEGER, limits TEXT)"
            )

    @contextmanager
    def session(self, thread_id: str):
        # Intentionally serialize this small service instance (including different threads)
        # to avoid concurrent access to its SQLite checkpoint connection.
        with self._session_lock, database_lock(self.db_path, "thread:" + thread_id):
            yield

    def start(self, thread_id: str):
        with self._lock, self._db:
            self._db.execute(
                "INSERT OR REPLACE INTO execution_turns VALUES (?, 0, 0, ?)",
                (thread_id, json.dumps(asdict(self.limits))),
            )

    def consume(self, thread_id: str, kind: str, deadline: float | None):
        check_deadline(deadline)
        if kind not in {"model_calls", "tool_calls"}:
            raise ValueError("Unknown execution counter")
        with self._lock, self._db:
            self._db.execute("BEGIN IMMEDIATE")
            row = self._db.execute("SELECT * FROM execution_turns WHERE thread_id=?", (thread_id,)).fetchone()
            if not row or json.loads(row[3]) != asdict(self.limits):
                raise ExecutionStopped("execution_configuration_changed")
            count = row[1 if kind == "model_calls" else 2]
            if count >= getattr(self.limits, kind):
                raise ExecutionStopped(kind + "_exhausted")
            self._db.execute(f"UPDATE execution_turns SET {kind}={kind}+1 WHERE thread_id=?", (thread_id,))

    def counts(self, thread_id: str) -> dict:
        with self._lock:
            row = self._db.execute(
                "SELECT model_calls, tool_calls FROM execution_turns WHERE thread_id=?", (thread_id,)
            ).fetchone()
        return dict(zip(("model_calls", "tool_calls"), row or (0, 0), strict=True))

    def close(self):
        self._db.close()
