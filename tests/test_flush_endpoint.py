"""Endpoint-guided membrane flush.

The flush endpoint is judged from effluent turbidity and residual drug. A round
extends itself while dirty, stops and reports at the extension ceiling, and is
fail-closed on missing or untrusted samples. Extension and over-limit decisions
are idempotent under concurrent triggers. The ceiling is versioned policy: a
round is pinned to the version at its start, and every decision is audited with
its basis and version for the console.
"""

import json
import threading

import pytest

from desalplant.audit.recorder import Recorder
from desalplant.clock import LogicalClock
from desalplant.errors import FlushRunNotStartedError, InvalidLimitsError
from desalplant.flush.limits import EndpointLimits
from desalplant.flush.run import FlushOutcome, FlushStatus
from desalplant.flush.sample import FlushSample
from desalplant.flush.service import FlushService
from desalplant.store.store import Store

CLEAN = FlushSample(turbidity=0.2, residual=0.1)
DIRTY = FlushSample(turbidity=5.0, residual=3.0)

SMALL = EndpointLimits(
    version=1,
    turbidity_limit=1.0,
    residual_limit=0.5,
    extend_seconds=5,
    max_seconds=20,
)


@pytest.fixture()
def recorder(store):
    return Recorder(store, LogicalClock())


@pytest.fixture()
def service(recorder):
    return FlushService(recorder, limits=SMALL, base_seconds=10)


def make_service(store, limits=SMALL, base_seconds=10):
    recorder = Recorder(store, LogicalClock())
    return FlushService(recorder, limits=limits, base_seconds=base_seconds), recorder


def kinds(recorder):
    return [event.kind for event in recorder.events()]


def basis(recorder, kind):
    event = next(event for event in recorder.events() if event.kind == kind)
    return json.loads(event.detail)


# --------------------------------------------------------------- endpoint

def test_clean_sample_releases_without_extension(service, recorder):
    service.start_run()
    decision = service.evaluate(CLEAN)
    assert decision.outcome is FlushOutcome.CLEAN
    assert decision.extensions == 0
    assert service.run.status is FlushStatus.CLEAN
    assert kinds(recorder) == ["flush/clean", "flush/start"]


def test_dirty_sample_extends_within_ceiling(service, recorder):
    service.start_run()
    first = service.evaluate(DIRTY)
    assert first.outcome is FlushOutcome.EXTENDED
    assert first.extensions == 1
    assert first.elapsed_seconds == 15
    assert first.added_seconds == 5
    second = service.evaluate(DIRTY)
    assert second.outcome is FlushOutcome.EXTENDED
    assert second.extensions == 2
    assert second.elapsed_seconds == 20


def test_dirty_past_ceiling_stops_and_reports(service, recorder):
    service.start_run()
    service.evaluate(DIRTY)
    service.evaluate(DIRTY)
    over = service.evaluate(DIRTY)
    assert over.outcome is FlushOutcome.OVER_LIMIT
    assert over.reason.startswith("extension ceiling")
    assert service.run.status is FlushStatus.OVER_LIMIT
    assert service.run.extensions == 2
    assert "flush/over_limit" in kinds(recorder)


def test_default_limits_extend_eight_times_then_stop(store):
    service, _ = make_service(store, limits=None, base_seconds=60)
    service.start_run()
    decision = None
    for index in range(20):
        decision = service.evaluate(DIRTY, key=f"k{index}")
        if decision.outcome is FlushOutcome.OVER_LIMIT:
            break
    assert service.run.extensions == 8
    assert service.run.elapsed_seconds == 300
    assert decision.outcome is FlushOutcome.OVER_LIMIT


# ------------------------------------------------- partial failure (fail closed)

def test_missing_sample_is_held_not_released(service, recorder):
    service.start_run()
    decision = service.evaluate(None)
    assert decision.outcome is FlushOutcome.HELD
    assert decision.idempotent is False
    assert service.run.status is FlushStatus.RUNNING
    assert service.run.extensions == 0
    assert "flush/sample_invalid" in kinds(recorder)


def test_untrusted_sample_is_held_not_released(service, recorder):
    service.start_run()
    decision = service.evaluate(FlushSample(turbidity=0.1, residual=0.05, trusted=False))
    assert decision.outcome is FlushOutcome.HELD
    assert service.run.status is FlushStatus.RUNNING
    assert service.run.extensions == 0


def test_held_then_clean_releases(service):
    service.start_run()
    service.evaluate(None)
    decision = service.evaluate(CLEAN)
    assert decision.outcome is FlushOutcome.CLEAN


def test_held_then_dirty_still_extends(service):
    service.start_run()
    service.evaluate(None)
    decision = service.evaluate(DIRTY)
    assert decision.outcome is FlushOutcome.EXTENDED
    assert decision.extensions == 1


# ------------------------------------------------------------- idempotency

def test_same_key_extends_once(service, recorder):
    service.start_run()
    first = service.evaluate(DIRTY, key="same")
    second = service.evaluate(DIRTY, key="same")
    assert first.outcome is FlushOutcome.EXTENDED
    assert second.idempotent is True
    assert service.run.extensions == 1
    assert kinds(recorder).count("flush/extended") == 1


def test_concurrent_same_key_extends_once(service, recorder):
    service.start_run()
    decisions = []

    def worker():
        decisions.append(service.evaluate(DIRTY, key="concurrent"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert service.run.extensions == 1
    assert kinds(recorder).count("flush/extended") == 1
    assert all(decision.outcome is FlushOutcome.EXTENDED for decision in decisions)


def test_concurrent_over_limit_reports_once(service, recorder):
    service.start_run()
    service.evaluate(DIRTY, key="p1")
    service.evaluate(DIRTY, key="p2")
    decisions = []

    def worker():
        decisions.append(service.evaluate(DIRTY, key="over"))

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert service.run.status is FlushStatus.OVER_LIMIT
    assert service.run.extensions == 2
    assert kinds(recorder).count("flush/over_limit") == 1


def test_terminal_round_never_extends_again(service, recorder):
    service.start_run()
    service.evaluate(DIRTY)
    service.evaluate(DIRTY)
    service.evaluate(DIRTY)
    before = recorder.count()
    decision = service.evaluate(DIRTY, key="retry")
    assert decision.idempotent is True
    assert decision.outcome is FlushOutcome.OVER_LIMIT
    assert recorder.count() == before
    assert service.run.extensions == 2


def test_clean_round_never_extends_again(service, recorder):
    service.start_run()
    service.evaluate(CLEAN)
    before = recorder.count()
    decision = service.evaluate(DIRTY, key="retry")
    assert decision.idempotent is True
    assert decision.outcome is FlushOutcome.CLEAN
    assert recorder.count() == before


# ------------------------------------------------------------- versioning

def test_running_round_pins_version_at_start(store):
    service, _ = make_service(store)
    service.start_run()
    run = service.run
    assert run.limits.version == 1
    service.migrate(
        turbidity_limit=2.0,
        residual_limit=0.9,
        extend_seconds=10,
        max_seconds=60,
    )
    assert run.limits.version == 1
    assert run.limits.max_seconds == 20
    assert service.current_limits.version == 2


def test_new_round_uses_migrated_version(store):
    service, _ = make_service(store)
    service.migrate(
        turbidity_limit=2.0,
        residual_limit=0.9,
        extend_seconds=10,
        max_seconds=60,
    )
    service.start_run()
    assert service.run.limits.version == 2
    assert service.run.limits.max_seconds == 60


def test_migrate_stamps_next_version(store):
    service, _ = make_service(store)
    migrated = service.migrate(
        turbidity_limit=2.0,
        residual_limit=0.9,
        extend_seconds=10,
        max_seconds=60,
    )
    assert migrated.version == 2
    again = service.migrate(
        turbidity_limit=3.0,
        residual_limit=1.0,
        extend_seconds=15,
        max_seconds=90,
    )
    assert again.version == 3


def test_migrate_is_audited_with_both_versions(store, recorder):
    service, _ = make_service(store)
    service.migrate(
        turbidity_limit=2.0,
        residual_limit=0.9,
        extend_seconds=10,
        max_seconds=60,
    )
    detail = basis(recorder, "flush/migrate")
    assert detail["previous_version"] == 1
    assert detail["version"] == 2
    assert detail["max_seconds"] == 60


# ----------------------------------------------------------------- audit

def test_clean_audit_carries_basis(store, recorder):
    service, _ = make_service(store)
    service.start_run()
    service.evaluate(CLEAN)
    detail = basis(recorder, "flush/clean")
    assert detail["turbidity"] == 0.2
    assert detail["residual"] == 0.1
    assert detail["turbidity_limit"] == 1.0
    assert detail["residual_limit"] == 0.5
    assert detail["limits_version"] == 1
    assert detail["max_seconds"] == 20


def test_over_limit_audit_carries_basis(store, recorder):
    service, _ = make_service(store)
    service.start_run()
    service.evaluate(DIRTY, key="a")
    service.evaluate(DIRTY, key="b")
    service.evaluate(DIRTY, key="c")
    detail = basis(recorder, "flush/over_limit")
    assert detail["max_seconds"] == 20
    assert detail["limits_version"] == 1
    assert detail["turbidity"] == 5.0


def test_replays_do_not_write_audit_events(service, recorder):
    service.start_run()
    service.evaluate(DIRTY, key="same")
    service.evaluate(DIRTY, key="same")
    service.evaluate(DIRTY, key="same")
    assert kinds(recorder).count("flush/extended") == 1


# ------------------------------------------------------------- validation

def test_evaluate_without_start_raises(recorder):
    service = FlushService(recorder)
    with pytest.raises(FlushRunNotStartedError):
        service.evaluate(CLEAN)


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(version=0, turbidity_limit=1.0, residual_limit=0.5, extend_seconds=5, max_seconds=20),
        dict(version=1, turbidity_limit=-1.0, residual_limit=0.5, extend_seconds=5, max_seconds=20),
        dict(version=1, turbidity_limit=1.0, residual_limit=0.5, extend_seconds=0, max_seconds=20),
        dict(version=1, turbidity_limit=1.0, residual_limit=0.5, extend_seconds=5, max_seconds=0),
    ],
)
def test_invalid_limits_rejected(kwargs):
    with pytest.raises(InvalidLimitsError):
        EndpointLimits(**kwargs)


# ------------------------------------------------------------- console

def test_console_endpoint_view(console):
    from support import call

    view = call(console, "GET", "/api/flush/endpoint")
    assert view["run"] is None
    assert view["limits"]["version"] == 1

    call(console, "POST", "/api/flush/start")
    call(console, "POST", "/api/flush/evaluate",
         {"turbidity": 5.0, "residual": 3.0, "key": "e1"})
    view = call(console, "GET", "/api/flush/endpoint")
    assert view["run"]["status"] == "running"
    assert view["run"]["limits_version"] == 1
    assert view["run"]["extensions"] == 1
    assert view["run"]["last_decision"]["outcome"] == "extended"


def test_console_migrate_then_new_round_uses_new_version(console):
    from support import call

    migrated = call(console, "POST", "/api/flush/migrate",
                    {"turbidity_limit": 2.0, "residual_limit": 0.9,
                     "extend_seconds": 10, "max_seconds": 120})
    assert migrated["limits"]["version"] == 2

    started = call(console, "POST", "/api/flush/start")
    assert started["run"]["limits_version"] == 2

    decision = call(console, "POST", "/api/flush/evaluate",
                    {"turbidity": 5.0, "residual": 3.0, "key": "f1"})
    assert decision["decision"]["limits_version"] == 2
    assert decision["decision"]["outcome"] == "extended"


def test_console_audit_events_carry_basis_and_version(console):
    from support import call

    call(console, "POST", "/api/flush/start")
    call(console, "POST", "/api/flush/evaluate",
         {"turbidity": 0.2, "residual": 0.1, "key": "e1"})
    call(console, "POST", "/api/flush/migrate",
         {"turbidity_limit": 2.0, "residual_limit": 0.9,
          "extend_seconds": 10, "max_seconds": 120})
    call(console, "POST", "/api/flush/start")
    call(console, "POST", "/api/flush/evaluate",
         {"turbidity": 5.0, "residual": 3.0, "key": "f1"})

    events = call(console, "GET", "/api/events?limit=50")["events"]
    by_kind = {}
    for event in events:
        by_kind.setdefault(event["kind"], []).append(json.loads(event["detail"]))

    clean = by_kind["flush/clean"][0]
    assert clean["limits_version"] == 1
    assert clean["turbidity"] == 0.2
    assert clean["residual"] == 0.1

    extended = by_kind["flush/extended"][0]
    assert extended["limits_version"] == 2

    migrate = by_kind["flush/migrate"][0]
    assert migrate["previous_version"] == 1
    assert migrate["version"] == 2
