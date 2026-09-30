"""Flush runs: idempotent transitions, concurrency, version pinning and audit."""

import threading

import pytest

from desalplant.audit import query
from desalplant.audit.recorder import Recorder
from desalplant.clock import LogicalClock
from desalplant.errors import (
    FlushIncompleteError,
    FlushLimitExceededError,
    FlushLimitVersionError,
    UnknownKeyError,
)
from desalplant.flush import events
from desalplant.flush.limits import FlushLimits
from desalplant.flush.sample import FlushSample
from desalplant.flush.supervisor import FlushSupervisor, decode_detail
from desalplant.store.store import Store


def build_supervisor(path: str | None = None) -> FlushSupervisor:
    store = Store(path)
    return FlushSupervisor(store, LogicalClock(), Recorder(store))


def sample(turbidity: float | None, residual: float | None, request_id: str, **extra) -> FlushSample:
    row = {"request_id": request_id}
    if turbidity is not None:
        row["turbidity"] = turbidity
    if residual is not None:
        row["residual"] = residual
    row.update(extra)
    return FlushSample.from_mapping(row)


CLEAN = dict(turbidity=0.2, residual=0.1)
DIRTY = dict(turbidity=3.0, residual=0.1)


def test_start_pins_the_current_limits_version():
    sup = build_supervisor()
    receipt = sup.start_run()
    assert receipt.created is True
    run = sup.active_run()
    assert run is not None
    assert run.limits.version == 1
    assert sup.current_limits().version == 1
    view = sup.view()
    assert view["state"] == "running"
    assert view["limits_version"] == 1
    assert view["audit"][0]["kind"] == events.STARTED
    payload = view["audit"][0]["payload"]
    assert payload["limits_version"] == 1
    assert payload["cap_seconds"] == 90


def test_starting_twice_is_idempotent():
    sup = build_supervisor()
    first = sup.start_run("round-1")
    second = sup.start_run("round-1")
    assert first.created is True
    assert second.created is False
    assert first.run is second.run


def test_clean_sample_releases_and_the_gate_clears():
    sup = build_supervisor()
    sup.start_run()
    verdict = sup.evaluate(sample(request_id="s1", **CLEAN))
    assert verdict.released() is True
    released = sup.release()
    assert released.released() is True
    view = sup.view()
    assert view["state"] == "released"
    kinds = [item["kind"] for item in view["audit"]]
    assert kinds == [events.STARTED, events.RELEASED]


def test_release_is_refused_while_running():
    sup = build_supervisor()
    sup.start_run()
    with pytest.raises(FlushIncompleteError):
        sup.release()


def test_extension_is_granted_until_the_cap_then_the_run_trips():
    sup = build_supervisor()
    sup.start_run()
    for used in range(1, 4):
        verdict = sup.evaluate(sample(request_id=f"d{used}", **DIRTY))
        assert verdict.outcome == "extend"
        # The verdict is stamped with the budget state before this extension.
        assert verdict.extensions_used == used - 1
    assert sup.active_run().extensions_used == 3
    verdict = sup.evaluate(sample(request_id="d4", **DIRTY))
    assert verdict.exceeded() is True
    view = sup.view()
    assert view["state"] == "exceeded"
    assert view["extended_seconds"] == 90
    with pytest.raises(FlushLimitExceededError):
        sup.release()


def test_repeated_request_id_extends_only_once_even_when_concurrent():
    sup = build_supervisor()
    sup.start_run()
    shared = sample(request_id="dup-1", **DIRTY)
    verdicts: list = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        verdicts.append(sup.evaluate(shared))

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    run = sup.active_run()
    assert run.extensions_used == 1
    assert run.decision_count() == 1
    assert all(v is verdicts[0] for v in verdicts)
    view = sup.view()
    extend_events = [item for item in view["audit"] if item["kind"] == events.EXTENDED]
    assert len(extend_events) == 1
    assert extend_events[0]["payload"]["sample"]["request_id"] == "dup-1"


def test_limit_trip_reporting_is_idempotent_under_concurrency():
    sup = build_supervisor()
    sup.start_run()
    for i in range(3):
        sup.evaluate(sample(request_id=f"d{i}", **DIRTY))
    final = sample(request_id="final", **DIRTY)
    verdicts: list = []
    barrier = threading.Barrier(6)

    def worker():
        barrier.wait()
        verdicts.append(sup.evaluate(final))

    threads = [threading.Thread(target=worker) for _ in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    run = sup.active_run()
    assert run.decision_count() == 4
    assert all(v.exceeded() for v in verdicts)
    view = sup.view()
    trips = [item for item in view["audit"] if item["kind"] == events.EXCEEDED]
    assert len(trips) == 1
    sequences = [event.sequence for event in sup.recorder.events()]
    assert len(sequences) == len(set(sequences))


def test_missing_sample_never_releases_even_when_values_would_pass():
    sup = build_supervisor()
    sup.start_run()
    verdict = sup.evaluate(sample(turbidity=0.2, residual=None, request_id="p1"))
    assert verdict.outcome == "extend"
    assert sup.active_run().running() is True


def test_untrusted_clean_value_never_releases():
    sup = build_supervisor()
    sup.start_run()
    verdict = sup.evaluate(
        FlushSample.from_mapping(
            {"request_id": "p2", "turbidity": {"value": 0.1, "trusted": False}, "residual": 0.1}
        )
    )
    assert verdict.outcome == "extend"


def test_partial_failures_consume_the_extension_budget():
    sup = build_supervisor()
    sup.start_run()
    for i in range(3):
        verdict = sup.evaluate(sample(turbidity=None, residual=None, request_id=f"m{i}"))
        assert verdict.outcome == "extend"
    verdict = sup.evaluate(sample(turbidity=None, residual=None, request_id="m3"))
    assert verdict.exceeded() is True
    with pytest.raises(FlushLimitExceededError):
        sup.release()


def test_samples_after_a_terminal_run_replay_the_final_verdict():
    sup = build_supervisor()
    sup.start_run()
    clean = sup.evaluate(sample(request_id="s1", **CLEAN))
    again = sup.evaluate(sample(request_id="s2", **DIRTY))
    assert again is clean
    assert sup.active_run().decision_count() == 1


def test_a_running_round_keeps_its_start_version_after_a_migration(tmp_path):
    store = Store(str(tmp_path / "plant.json"))
    clock = LogicalClock()
    recorder = Recorder(store, clock)
    sup = FlushSupervisor(store, clock, recorder)
    sup.start_run("round-1")
    sup.evaluate(sample(request_id="d1", **DIRTY))

    migrated = sup.revise(max_extensions=10, extend_seconds=60)
    assert migrated.version == 2
    assert sup.current_limits().max_extensions == 10

    running = sup.run("round-1")
    assert running.limits.version == 1
    assert running.extensions_used == 1
    view = sup.view("round-1")
    assert view["limits_version"] == 1
    assert view["cap_seconds"] == 90

    sup.start_run("round-2")
    assert sup.run("round-2").limits.version == 2


def test_publishing_a_version_out_of_order_is_rejected():
    sup = build_supervisor()
    bad = FlushLimits(
        version=5,
        max_turbidity=1.0,
        max_residual=0.5,
        extend_seconds=30,
        max_extensions=2,
    )
    with pytest.raises(FlushLimitVersionError):
        sup.publish_limits(bad)


def test_unknown_run_and_revision_are_reported():
    sup = build_supervisor()
    with pytest.raises(UnknownKeyError):
        sup.run("nope")
    with pytest.raises(UnknownKeyError):
        sup.limits_at(99)


def test_migration_is_audited_with_old_and_new_versions(tmp_path):
    store = Store(str(tmp_path / "plant.json"))
    sup = FlushSupervisor(store, LogicalClock(), Recorder(store))
    sup.revise(max_turbidity=2.0)
    event = sup.recorder.latest(events.LIMITS_REVISED)
    assert event is not None
    payload = decode_detail(event.detail)
    assert payload["from_version"] == 1
    assert payload["limits"]["version"] == 2
    assert payload["limits"]["max_turbidity"] == 2.0
    revisions = sup.revisions()
    assert [row["version"] for row in revisions] == [1, 2]


def test_failed_persistence_rolls_the_run_state_back():
    from desalplant.errors import StoreError

    sup = build_supervisor()
    sup.start_run("round-1")

    def fail_persist(run_id, snapshot):
        raise StoreError("disk gone")

    sup.run("round-1").bind(persister=fail_persist)
    with pytest.raises(StoreError):
        sup.evaluate(sample(request_id="d1", **DIRTY))

    run = sup.run("round-1")
    assert run.running() is True
    assert run.extensions_used == 0
    assert run.decision_count() == 0
    assert "d1" not in run._pending
    # A retry with a working persister still grants the extension once.
    sup.run("round-1").bind(persister=sup._persist_run)
    verdict = sup.evaluate(sample(request_id="d1", **DIRTY))
    assert verdict.outcome == "extend"
    assert run.extensions_used == 1


def test_run_survives_a_supervisor_resume_from_the_store(tmp_path):
    path = str(tmp_path / "plant.json")
    store = Store(path)
    sup = FlushSupervisor(store, LogicalClock(), Recorder(store))
    sup.start_run("round-1")
    sup.evaluate(sample(request_id="d1", **DIRTY))

    resumed_store = Store(path)
    resumed = FlushSupervisor(resumed_store, LogicalClock(), Recorder(resumed_store))
    assert resumed.current_limits().version == 1
    persisted = resumed_store.require("flush/run/round-1")
    assert persisted["state"] == "running"
    assert persisted["extensions_used"] == 1
    assert persisted["decisions"][0]["outcome"] == "extend"


def test_audit_sequences_are_contiguous_under_mixed_concurrency():
    sup = build_supervisor()
    sup.start_run()
    barrier = threading.Barrier(12)

    def worker(index):
        barrier.wait()
        sup.evaluate(sample(request_id=f"m-{index}", **DIRTY))

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    sequences = [event.sequence for event in query.list_events(sup.store, sup.recorder.sequence)]
    # list_events is newest-first; the point is that no sequence was lost or doubled.
    assert sorted(sequences) == list(range(1, len(sequences) + 1))
    # Only the first three distinct requests grant extensions; the fourth trips.
    view = sup.view()
    assert view["extensions_used"] == 3
    assert view["state"] == "exceeded"
