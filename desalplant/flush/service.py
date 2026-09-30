"""Endpoint flush service: versioned limits, rounds and the audit trail."""

import json
from typing import Any

from ..audit.recorder import Recorder
from ..clock import LogicalClock
from ..errors import FlushRunNotStartedError
from .limits import EndpointLimits
from .registry import LimitRegistry
from .run import FlushDecision, FlushOutcome, FlushRun
from .sample import FlushSample

DEFAULT_LIMITS = EndpointLimits(
    version=1,
    turbidity_limit=1.0,
    residual_limit=0.5,
    extend_seconds=30,
    max_seconds=300,
)
DEFAULT_BASE_SECONDS = 60


class FlushService:
    """Coordinates endpoint-guided rounds against a versioned limits registry.

    Every decision is audited with its basis (the readings and the limits they
    were judged against) and the limits version, so the console can show why a
    round was released, extended or stopped. Replays never write a second event.
    """

    def __init__(
        self,
        recorder: Recorder,
        clock: LogicalClock | None = None,
        limits: EndpointLimits | None = None,
        base_seconds: int = DEFAULT_BASE_SECONDS,
    ) -> None:
        self._recorder = recorder
        self._clock = clock
        self.registry = LimitRegistry(limits or DEFAULT_LIMITS)
        self.base_seconds = base_seconds
        self._run: FlushRun | None = None
        self._run_seq = 0

    @property
    def current_limits(self) -> EndpointLimits:
        """Generation applied to rounds started right now."""
        return self.registry.current

    @property
    def run(self) -> FlushRun | None:
        """Active round, or ``None`` before the first start."""
        return self._run

    def start_run(self, base_seconds: int | None = None) -> FlushRun:
        """Start a round, pinning it to the limits generation in force now."""
        self._run_seq += 1
        run = FlushRun(
            run_id=f"flush-{self._run_seq:04d}",
            limits=self.registry.current,
            base_seconds=self.base_seconds if base_seconds is None else base_seconds,
        )
        self._run = run
        self._record(
            "flush/start",
            {
                "run_id": run.run_id,
                "limits_version": run.limits.version,
                "base_seconds": run.base_seconds,
                "max_seconds": run.limits.max_seconds,
            },
        )
        return run

    def evaluate(self, sample: FlushSample | None, key: str | None = None) -> FlushDecision:
        """Evaluate the active round and audit any new decision."""
        if self._run is None:
            raise FlushRunNotStartedError("no endpoint flush round has been started")
        decision = self._run.evaluate(sample, key)
        if not decision.idempotent:
            self._audit(decision)
        return decision

    def migrate(
        self,
        *,
        turbidity_limit: float,
        residual_limit: float,
        extend_seconds: int,
        max_seconds: int,
    ) -> EndpointLimits:
        """Adopt a new limits generation and audit the migration."""
        previous = self.registry.current
        migrated = self.registry.migrate(
            turbidity_limit=turbidity_limit,
            residual_limit=residual_limit,
            extend_seconds=extend_seconds,
            max_seconds=max_seconds,
        )
        self._record(
            "flush/migrate",
            {
                "previous_version": previous.version,
                "version": migrated.version,
                "turbidity_limit": migrated.turbidity_limit,
                "residual_limit": migrated.residual_limit,
                "extend_seconds": migrated.extend_seconds,
                "max_seconds": migrated.max_seconds,
            },
        )
        return migrated

    def view(self) -> dict[str, Any]:
        """Console view of the active round and its limits generation."""
        return {
            "limits": self.registry.current.to_dict(),
            "run": self._run.to_dict() if self._run is not None else None,
        }

    def _audit(self, decision: FlushDecision) -> None:
        kind = {
            FlushOutcome.CLEAN: "flush/clean",
            FlushOutcome.EXTENDED: "flush/extended",
            FlushOutcome.OVER_LIMIT: "flush/over_limit",
            FlushOutcome.HELD: "flush/sample_invalid",
        }[decision.outcome]
        self._record(kind, decision.to_dict())

    def _record(self, kind: str, payload: dict[str, Any]) -> None:
        self._recorder.log(kind, json.dumps(payload, sort_keys=True))
