"""Registry and concurrency boundary for turbidity-driven flush runs.

The supervisor owns the versioned limits document, keeps one active run, and
writes every decision and limits revision to the append-only audit record.
Each run carries its own lock, so concurrent extension and limit-trip
reporting converge on one idempotent transition; the supervisor lock only
guards the registry and is never held while a run lock is taken.
"""

import json
import threading
from dataclasses import dataclass
from typing import Any

from ..audit import query
from ..audit.recorder import Recorder
from ..clock import LogicalClock
from ..errors import (
    FlushIncompleteError,
    FlushLimitVersionError,
    UnknownKeyError,
)
from ..ns.ids import new_id
from ..store import keys
from ..store.store import Store
from . import events
from .endpoint import Verdict
from .limits import FlushLimits, revised
from .run import FlushRun
from .sample import FlushSample

DEFAULT_LIMITS = FlushLimits(
    max_turbidity=1.0,
    max_residual=0.5,
    extend_seconds=30,
    max_extensions=3,
    version=1,
)


@dataclass(frozen=True)
class StartReceipt:
    """Result of starting a run, including whether one already existed."""

    run: FlushRun
    created: bool
    event_sequence: int = 0


class FlushSupervisor:
    """Owns the limits revisions, the active run and the audit writes."""

    def __init__(
        self,
        store: Store,
        clock: LogicalClock,
        recorder: Recorder,
    ) -> None:
        self._store = store
        self._clock = clock
        self._recorder = recorder
        self._runs: dict[str, FlushRun] = {}
        self._active_id: str | None = None
        self._lock = threading.Lock()

    @property
    def recorder(self) -> Recorder:
        """Audit recorder shared with the rest of the plant."""
        return self._recorder

    @property
    def store(self) -> Store:
        """Persisted key space shared with the rest of the plant."""
        return self._store

    # -------------------------------------------------------------- limits

    def current_limits(self) -> FlushLimits:
        """The revision a newly started run has to use."""
        version = self._store.get(keys.FLUSH_LIMITS_CURRENT)
        if version is None:
            return DEFAULT_LIMITS
        row = self._store.get(keys.flush_limit_key(int(version)))
        if row is None:
            return DEFAULT_LIMITS
        return FlushLimits.from_dict(row)

    def limits_at(self, version: int) -> FlushLimits:
        """An immutable revision, or an error when it was never published."""
        row = self._store.get(keys.flush_limit_key(version))
        if row is None:
            raise UnknownKeyError(f"no flush limits revision {version}")
        return FlushLimits.from_dict(row)

    def revisions(self) -> list[dict[str, Any]]:
        """Every published revision, oldest first.

        Before the first explicit migration the built-in baseline is reported
        as the effective revision even though it was never written.
        """
        rows = [
            self._store.get(key)
            for key in self._store.keys(keys.FLUSH_LIMIT_VERSION_PREFIX)
        ]
        published = [FlushLimits.from_dict(row).to_dict() for row in rows if row is not None]
        return published if published else [DEFAULT_LIMITS.to_dict()]

    def publish_limits(self, next_limits: FlushLimits) -> FlushLimits:
        """Publish a revision; its version has to follow the current one.

        Runs already in flight keep the revision pinned at their start; the
        new revision only binds runs started afterwards.
        """
        with self._lock:
            self._ensure_seeded()
            current = self.current_limits()
            if next_limits.version != current.version + 1:
                raise FlushLimitVersionError(
                    f"limits revision {next_limits.version} does not follow "
                    f"the current revision {current.version}"
                )
            self._store.put(
                keys.flush_limit_key(next_limits.version), next_limits.to_dict()
            )
            self._store.put(keys.FLUSH_LIMITS_CURRENT, next_limits.version)
            self._recorder.log(
                events.LIMITS_REVISED,
                json.dumps(
                    {"from_version": current.version, "limits": next_limits.to_dict()},
                    sort_keys=True,
                ),
            )
            return next_limits

    def revise(self, **values: Any) -> FlushLimits:
        """Publish the next revision from changed threshold fields."""
        return self.publish_limits(revised(self.current_limits(), **values))

    def _seed(self, limits: FlushLimits) -> None:
        if limits.version != 1:
            raise FlushLimitVersionError("first flush limits revision must be version one")
        self._store.put(keys.flush_limit_key(limits.version), limits.to_dict())
        self._store.put(keys.FLUSH_LIMITS_CURRENT, limits.version)

    def _ensure_seeded(self) -> None:
        if self._store.get(keys.FLUSH_LIMITS_CURRENT) is None:
            self._seed(DEFAULT_LIMITS)

    # ---------------------------------------------------------------- runs

    def start_run(self, run_id: str | None = None) -> StartReceipt:
        """Start a run against the current revision.

        Reusing an id of an existing running run is a no-op, so starting the
        same round concurrently produces exactly one run.
        """
        with self._lock:
            existing = self._runs.get(run_id) if run_id else self._runs.get(self._active_id or "")
            if existing is not None and existing.running():
                return StartReceipt(run=existing, created=False)

            self._ensure_seeded()
            self._clock.advance()
            limits = self.current_limits()
            run = FlushRun(
                limits=limits,
                run_id=run_id or f"flush-{new_id()[:12]}",
                started_tick=self._clock.tick,
            )
            run.bind(auditor=self._audit, persister=self._persist_run)
            event = self._recorder.log(
                events.STARTED,
                json.dumps(
                    {
                        "run_id": run.run_id,
                        "limits_version": limits.version,
                        "cap_seconds": limits.cap_seconds(),
                        "limits": limits.to_dict(),
                    },
                    sort_keys=True,
                ),
            )
            run.event_sequences.append(event.sequence)
            self._runs[run.run_id] = run
            self._active_id = run.run_id
            self._persist_run(run.run_id, run.snapshot())
            return StartReceipt(run=run, created=True, event_sequence=event.sequence)

    def active_run(self) -> FlushRun | None:
        """The run that currently accepts samples, if any."""
        with self._lock:
            if self._active_id is None:
                return None
            return self._runs.get(self._active_id)

    def run(self, run_id: str) -> FlushRun:
        """Look up a run by id."""
        with self._lock:
            run = self._runs.get(run_id)
            if run is None:
                raise UnknownKeyError(f"no flush run {run_id}")
            return run

    def evaluate(self, sample: FlushSample, run_id: str | None = None) -> Verdict:
        """Feed a sample to the active run, or to a named run."""
        run = self.run(run_id) if run_id else self.require_active()
        return run.evaluate(sample)

    def require_active(self) -> FlushRun:
        """Return the active run, refusing when none is running."""
        run = self.active_run()
        if run is None:
            raise FlushIncompleteError("no flush run is active")
        return run

    def release(self, run_id: str | None = None) -> Verdict:
        """Gate used before clearing the flush: it must have reached endpoint."""
        run = self.run(run_id) if run_id else self.require_active()
        return run.require_releasable()

    def view(self, run_id: str | None = None) -> dict[str, Any]:
        """Console view of a run with its audit trail."""
        run = self.run(run_id) if run_id else self.require_active()
        return self._view(run)

    def list_runs(self) -> list[dict[str, Any]]:
        """Console view of every run, newest first."""
        with self._lock:
            runs = list(self._runs.values())
        return [self._view(run) for run in reversed(runs)]

    # ------------------------------------------------------------- internals

    def _audit(self, kind: str, verdict: Verdict) -> int:
        event = self._recorder.log(kind, json.dumps(verdict.to_dict(), sort_keys=True))
        return event.sequence

    def _persist_run(self, run_id: str, snapshot: dict[str, Any]) -> None:
        self._store.put(keys.flush_run_key(run_id), snapshot)

    def _view(self, run: FlushRun) -> dict[str, Any]:
        snapshot = run.snapshot()
        trail: list[dict[str, Any]] = []
        for sequence in snapshot["event_sequences"]:
            event = query.event_at(self._store, sequence)
            if event is None:
                continue
            row = event.to_dict()
            row["payload"] = decode_detail(row["detail"])
            trail.append(row)
        snapshot["audit"] = trail
        return snapshot


def decode_detail(detail: str) -> Any:
    """Decode a JSON audit detail, falling back to the raw text."""
    try:
        return json.loads(detail)
    except (TypeError, ValueError):
        return detail
