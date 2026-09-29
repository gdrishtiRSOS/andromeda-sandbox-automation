"""Run state held between the two phases, and the one-run-at-a-time lock.

Everything lives in memory. The app is local and single-user; if it is
restarted mid-way, "Resume an existing account" rebuilds what phase 2 needs
from the authority itself.

Tokens are never stored here. They are handed to the worker that needs them
and go out of scope with it.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any, Dict, Iterator, List, Optional

__all__ = ["QUIET", "Run", "RunBusyError", "Runs", "capture_logs", "scrub"]

#: Statuses in which nothing is running, so an event stream can close.
QUIET = frozenset({"previewed", "signup_failed", "awaiting_email",
                   "awaiting_continue", "done", "failed"})

_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")
_BEARER = re.compile(r"(?i)bearer\s+\S+")


def scrub(text: str) -> str:
    """Belt and braces: nothing token-shaped leaves the server in an event."""
    return _BEARER.sub("Bearer [redacted]", _JWT.sub("[redacted token]", text))


@dataclass
class Run:
    id: str
    kind: str                                    # "new" | "resume"
    status: str
    form: Dict[str, Any] = field(default_factory=dict)
    boundary_id: Optional[str] = None
    boundary: Optional[Dict[str, Any]] = None    # summary, for the page
    plan: Optional[Dict[str, Any]] = None
    blocked: bool = False
    authority: Optional[str] = None              # what configure_account looks up
    authority_id: Optional[str] = None
    organization_id: Optional[str] = None
    signup: Optional[Dict[str, Any]] = None
    email_confirmed: Optional[str] = None        # "link" | "clicked"
    capture_id: Optional[str] = None             # copied capability set, if any
    capabilities: Optional[Dict[str, Any]] = None  # what Preview showed about it
    capabilities_ack: bool = False               # unknown names acknowledged
    result: Optional[Dict[str, Any]] = None
    error: Optional[Dict[str, Any]] = None
    snapshots: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    events: List[Dict[str, Any]] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: dt.datetime.now().isoformat(timespec="seconds"))
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def emit(self, type_: str, **data: Any) -> None:
        # round-trip through JSON so what is stored is exactly what is sent
        payload = json.loads(json.dumps({"type": type_, **data}, default=str))
        with self._lock:
            payload["id"] = len(self.events)
            self.events.append(payload)

    def set_status(self, status: str) -> None:
        # event first: a stream that sees the new status must already be able
        # to see the event announcing it, or it could close without sending it
        self.emit("status", status=status)
        self.status = status

    def events_after(self, index: int) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.events[index + 1:])

    def public(self) -> Dict[str, Any]:
        return {
            "id": self.id, "kind": self.kind, "status": self.status,
            "form": self.form, "boundary": self.boundary, "plan": self.plan,
            "blocked": self.blocked, "authority": self.authority,
            "authority_id": self.authority_id, "organization_id": self.organization_id,
            "signup": self.signup, "email_confirmed": self.email_confirmed,
            "capture_id": self.capture_id, "capabilities": self.capabilities,
            "capabilities_ack": self.capabilities_ack,
            "result": self.result, "error": self.error,
            "snapshots": sorted(self.snapshots), "events": list(self.events),
        }


class RunBusyError(Exception):
    def __init__(self, active: Optional[str]):
        self.active = active
        super().__init__(f"run {active} is already writing; one run at a time")


class Runs:
    """Every run this process has seen, and the lock that serialises writes.

    Two runs writing at once would collide on the environment-wide revision,
    so anything that writes takes `start()`; a second caller is refused, not
    queued.
    """

    def __init__(self) -> None:
        self._runs: Dict[str, Run] = {}
        self._busy = threading.Lock()
        self.active: Optional[str] = None

    def new(self, kind: str, status: str, **fields: Any) -> Run:
        run = Run(id=uuid.uuid4().hex[:12], kind=kind, status=status, **fields)
        self._runs[run.id] = run
        return run

    def get(self, run_id: str) -> Optional[Run]:
        return self._runs.get(run_id)

    def start(self, run: Run) -> None:
        if not self._busy.acquire(blocking=False):
            raise RunBusyError(self.active)
        self.active = run.id

    def finish(self) -> None:
        self.active = None
        self._busy.release()


class _RunLogHandler(logging.Handler):
    """Forwards the modules' log records -- from one thread only -- to a run."""

    def __init__(self, run: Run, thread_id: int):
        super().__init__(level=logging.INFO)
        self.run = run
        self.thread_id = thread_id

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread != self.thread_id:
            return
        try:
            message = scrub(record.getMessage())
        except Exception:
            message = scrub(str(record.msg))
        self.run.emit("log", level=record.levelname, logger=record.name, message=message)


@contextmanager
def capture_logs(run: Run) -> Iterator[None]:
    """Stream the logic package's logging into `run` for the duration."""
    logger = logging.getLogger("logic")
    if logger.getEffectiveLevel() > logging.INFO:
        logger.setLevel(logging.INFO)
    handler = _RunLogHandler(run, threading.get_ident())
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
