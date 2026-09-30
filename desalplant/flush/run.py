"""One flush run: a state machine driven by endpoint samples.

Extension and limit-trip reporting can be requested concurrently for the same
sample. Every state transition happens inside the run lock and is idempotent:
a repeated request identifier returns the original verdict, a terminal run
replays its final verdict, and the audit hook fires at most once per decision.
"""

from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Callable

from ..errors import FlushIncompleteError, FlushLimitExceededError
from ..ns.ids import new_id
from .endpoint import (
    EXTEND,
    RELEASE,
    Verdict,
    judge,
)
from .limits import FlushLimits
from .sample import FlushSample

RUNNING = "running"
RELEASED = "released"
EXCEEDED_STATE = "exceeded"

Auditor = Callable[[str, "Verdict"], int]
Persister = Callable[[str, dict[str, Any]], None]


@dataclass
class FlushRun:
    """State, envelope and decision history of one flush run."""

    limits: FlushLimits
    run_id: str = field(default_factory=new_id)
    started_tick: int = 0
    state: str = RUNNING
    extensions_used: int = 0
    extended_seconds: int = 0
    verdicts: list[Verdict] = field(default_factory=list)
    event_sequences: list[int] = field(default_factory=list)
    _lock: RLock = field(default_factory=RLock, repr=False)
    _pending: dict[str, Verdict] = field(default_factory=dict, repr=False)
    _auditor: Auditor | None = field(default=None, repr=False)
    _persister: Persister | None = field(default=None, repr=False)

    def bind(self, auditor: Auditor | None = None, persister: Persister | None = None) -> None:
        """Attach the audit and persistence hooks owned by the supervisor."""
        with self._lock:
            if auditor is not None:
                self._auditor = auditor
            if persister is not None:
                self._persister = persister

    def evaluate(self, sample: FlushSample) -> Verdict:
        """Apply one sample, extending, releasing or tripping exactly once."""
        with self._lock:
            if self.state != RUNNING:
                return self.verdicts[-1]
            request_id = sample.request_id or sample.sample_id
            if request_id and request_id in self._pending:
                return self._pending[request_id]

            verdict = judge(sample, self.limits, self.extensions_used)
            checkpoint = self._checkpoint(request_id)
            kind = ""
            if verdict.outcome == EXTEND:
                self.extensions_used += 1
                self.extended_seconds += self.limits.extend_seconds
                kind = "flush.extended"
            elif verdict.outcome == RELEASE:
                self.state = RELEASED
                kind = "flush.released"
            else:
                self.state = EXCEEDED_STATE
                kind = "flush.exceeded"

            sequence = self._record(kind, verdict)
            self.verdicts.append(verdict)
            if request_id:
                self._pending[request_id] = verdict
            self.event_sequences.append(sequence)
            try:
                self._persist()
            except Exception:
                checkpoint.restore()
                raise
            return verdict

    def _checkpoint(self, pending_key: str) -> "_RunCheckpoint":
        return _RunCheckpoint(
            run=self,
            state=self.state,
            extensions_used=self.extensions_used,
            extended_seconds=self.extended_seconds,
            verdicts=len(self.verdicts),
            event_sequences=len(self.event_sequences),
            pending_key=pending_key,
        )

    def require_releasable(self) -> Verdict:
        """Return the release verdict or refuse to clear the flush."""
        with self._lock:
            if self.state == EXCEEDED_STATE:
                raise FlushLimitExceededError(
                    f"flush run {self.run_id} exceeded its extension cap"
                )
            if self.state != RELEASED:
                raise FlushIncompleteError(
                    f"flush run {self.run_id} has not reached its endpoint"
                )
            return self.verdicts[-1]

    def running(self) -> bool:
        """Whether the run still accepts samples."""
        with self._lock:
            return self.state == RUNNING

    def last_verdict(self) -> Verdict | None:
        """Newest decision recorded for the run."""
        with self._lock:
            return self.verdicts[-1] if self.verdicts else None

    def decision_count(self) -> int:
        """How many distinct decisions were recorded."""
        with self._lock:
            return len(self.verdicts)

    def _record(self, kind: str, verdict: Verdict) -> int:
        if self._auditor is None:
            return 0
        return self._auditor(kind, verdict)

    def _persist(self) -> None:
        if self._persister is not None:
            self._persister(self.run_id, self.snapshot())

    def snapshot(self) -> dict[str, Any]:
        """JSON-friendly view of the run state."""
        with self._lock:
            return {
                "run_id": self.run_id,
                "state": self.state,
                "limits_version": self.limits.version,
                "started_tick": self.started_tick,
                "extensions_used": self.extensions_used,
                "extended_seconds": self.extended_seconds,
                "cap_seconds": self.limits.cap_seconds(),
                "decisions": [verdict.to_dict() for verdict in self.verdicts],
                "event_sequences": list(self.event_sequences),
            }


@dataclass
class _RunCheckpoint:
    """In-memory state to restore when a decision fails to persist."""

    run: FlushRun
    state: str
    extensions_used: int
    extended_seconds: int
    verdicts: int
    event_sequences: int
    pending_key: str

    def restore(self) -> None:
        run = self.run
        run.state = self.state
        run.extensions_used = self.extensions_used
        run.extended_seconds = self.extended_seconds
        del run.verdicts[self.verdicts:]
        del run.event_sequences[self.event_sequences:]
        if self.pending_key:
            run._pending.pop(self.pending_key, None)
