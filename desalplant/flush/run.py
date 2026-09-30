"""A single endpoint-guided flush round.

The round is pinned, at start, to the limits generation that was active. It
extends itself while the effluent is still dirty, stops cleanly at the
endpoint, and stops and reports once the extension ceiling is reached.

Evaluations are serialized and keyed so that concurrent triggers of the same
decision extend at most once and never report the ceiling twice. A missing or
untrusted sample is a partial failure and is fail-closed: it can never release
the round as clean.
"""

from dataclasses import dataclass
from enum import Enum
from threading import Lock
from typing import Any

from .limits import EndpointLimits
from .sample import FlushSample, usable


class FlushOutcome(str, Enum):
    """What one evaluation decided."""

    CLEAN = "clean"  # endpoint reached, round released clean
    EXTENDED = "extended"  # still dirty, one extension applied
    OVER_LIMIT = "over_limit"  # ceiling reached, stopped and reported
    HELD = "held"  # partial failure: not released, still running


class FlushStatus(str, Enum):
    """Lifecycle of a round."""

    RUNNING = "running"
    CLEAN = "clean"
    OVER_LIMIT = "over_limit"


@dataclass(frozen=True)
class FlushDecision:
    """The result of evaluating one sample.

    Carries the full basis (the readings and the limits they were judged
    against) plus the limits version, so the audit trail can show *why* the
    round was released, extended or stopped.
    """

    outcome: FlushOutcome
    idempotent: bool
    run_id: str
    limits_version: int
    extensions: int
    elapsed_seconds: int
    added_seconds: int
    turbidity: float | None
    residual: float | None
    sample_trusted: bool | None
    turbidity_limit: float
    residual_limit: float
    max_seconds: int
    reason: str

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection used by the console and audit."""
        return {
            "outcome": self.outcome.value,
            "idempotent": self.idempotent,
            "run_id": self.run_id,
            "limits_version": self.limits_version,
            "extensions": self.extensions,
            "elapsed_seconds": self.elapsed_seconds,
            "added_seconds": self.added_seconds,
            "turbidity": self.turbidity,
            "residual": self.residual,
            "sample_trusted": self.sample_trusted,
            "turbidity_limit": self.turbidity_limit,
            "residual_limit": self.residual_limit,
            "max_seconds": self.max_seconds,
            "reason": self.reason,
        }


class FlushRun:
    """Endpoint-guided flush state machine for one round."""

    def __init__(self, run_id: str, limits: EndpointLimits, base_seconds: int) -> None:
        if base_seconds <= 0:
            raise ValueError("base flush seconds must be positive")
        self.run_id = run_id
        self.limits = limits
        self.base_seconds = base_seconds
        self.extensions = 0
        self.status = FlushStatus.RUNNING
        self.last_decision: FlushDecision | None = None
        self._lock = Lock()
        self._decisions: dict[str, FlushDecision] = {}
        self._eval_seq = 0

    @property
    def elapsed_seconds(self) -> int:
        """Total flush time accumulated so far."""
        return self.base_seconds + self.extensions * self.limits.extend_seconds

    def evaluate(self, sample: FlushSample | None, key: str | None = None) -> FlushDecision:
        """Decide the endpoint for one sample.

        Concurrent triggers are safe: the same ``key`` replays the first
        decision without a new side effect, and a terminal round never extends
        or reports again. A missing or untrusted sample is held and can never
        release the round as clean.
        """
        with self._lock:
            if self.status is not FlushStatus.RUNNING:
                return self._terminal_decision()
            if key is None:
                key = self._next_key()
            existing = self._decisions.get(key)
            if existing is not None:
                return self._replay(existing)
            decision = self._decide(sample)
            self._decisions[key] = decision
            self.last_decision = decision
            return decision

    def _next_key(self) -> str:
        self._eval_seq += 1
        return f"{self.run_id}#eval-{self._eval_seq}"

    def _decide(self, sample: FlushSample | None) -> FlushDecision:
        limits = self.limits

        # Partial failure: no usable reading. Fail closed and never release.
        if not usable(sample):
            return FlushDecision(
                outcome=FlushOutcome.HELD,
                idempotent=False,
                run_id=self.run_id,
                limits_version=limits.version,
                extensions=self.extensions,
                elapsed_seconds=self.elapsed_seconds,
                added_seconds=0,
                turbidity=sample.turbidity if sample is not None else None,
                residual=sample.residual if sample is not None else None,
                sample_trusted=sample.trusted if sample is not None else None,
                turbidity_limit=limits.turbidity_limit,
                residual_limit=limits.residual_limit,
                max_seconds=limits.max_seconds,
                reason="sample missing or untrusted; endpoint cannot be confirmed",
            )

        if limits.clean(sample):
            self.status = FlushStatus.CLEAN
            return FlushDecision(
                outcome=FlushOutcome.CLEAN,
                idempotent=False,
                run_id=self.run_id,
                limits_version=limits.version,
                extensions=self.extensions,
                elapsed_seconds=self.elapsed_seconds,
                added_seconds=0,
                turbidity=sample.turbidity,
                residual=sample.residual,
                sample_trusted=True,
                turbidity_limit=limits.turbidity_limit,
                residual_limit=limits.residual_limit,
                max_seconds=limits.max_seconds,
                reason="effluent turbidity and residual drug within limits",
            )

        # Still dirty: extend while the ceiling still allows another step.
        if self.elapsed_seconds + limits.extend_seconds <= limits.max_seconds:
            self.extensions += 1
            return FlushDecision(
                outcome=FlushOutcome.EXTENDED,
                idempotent=False,
                run_id=self.run_id,
                limits_version=limits.version,
                extensions=self.extensions,
                elapsed_seconds=self.elapsed_seconds,
                added_seconds=limits.extend_seconds,
                turbidity=sample.turbidity,
                residual=sample.residual,
                sample_trusted=True,
                turbidity_limit=limits.turbidity_limit,
                residual_limit=limits.residual_limit,
                max_seconds=limits.max_seconds,
                reason="effluent still dirty; extended within the ceiling",
            )

        # Ceiling reached without a clean endpoint: stop flushing and report.
        self.status = FlushStatus.OVER_LIMIT
        return FlushDecision(
            outcome=FlushOutcome.OVER_LIMIT,
            idempotent=False,
            run_id=self.run_id,
            limits_version=limits.version,
            extensions=self.extensions,
            elapsed_seconds=self.elapsed_seconds,
            added_seconds=0,
            turbidity=sample.turbidity,
            residual=sample.residual,
            sample_trusted=True,
            turbidity_limit=limits.turbidity_limit,
            residual_limit=limits.residual_limit,
            max_seconds=limits.max_seconds,
            reason="extension ceiling reached before the effluent was clean",
        )

    def _terminal_decision(self) -> FlushDecision:
        """Replay for a round already decided; never a new side effect."""
        limits = self.limits
        outcome = (
            FlushOutcome.CLEAN
            if self.status is FlushStatus.CLEAN
            else FlushOutcome.OVER_LIMIT
        )
        return FlushDecision(
            outcome=outcome,
            idempotent=True,
            run_id=self.run_id,
            limits_version=limits.version,
            extensions=self.extensions,
            elapsed_seconds=self.elapsed_seconds,
            added_seconds=0,
            turbidity=None,
            residual=None,
            sample_trusted=None,
            turbidity_limit=limits.turbidity_limit,
            residual_limit=limits.residual_limit,
            max_seconds=limits.max_seconds,
            reason="round already decided; no further extension or report",
        )

    def _replay(self, decision: FlushDecision) -> FlushDecision:
        """A repeated evaluation of the same key: same decision, no new effect."""
        return FlushDecision(
            outcome=decision.outcome,
            idempotent=True,
            run_id=decision.run_id,
            limits_version=decision.limits_version,
            extensions=decision.extensions,
            elapsed_seconds=decision.elapsed_seconds,
            added_seconds=0,
            turbidity=decision.turbidity,
            residual=decision.residual,
            sample_trusted=decision.sample_trusted,
            turbidity_limit=decision.turbidity_limit,
            residual_limit=decision.residual_limit,
            max_seconds=decision.max_seconds,
            reason="duplicate evaluation replayed; no extension or report",
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection of the round for the console."""
        return {
            "run_id": self.run_id,
            "status": self.status.value,
            "limits_version": self.limits.version,
            "base_seconds": self.base_seconds,
            "extensions": self.extensions,
            "elapsed_seconds": self.elapsed_seconds,
            "max_seconds": self.limits.max_seconds,
            "last_decision": self.last_decision.to_dict() if self.last_decision else None,
        }
