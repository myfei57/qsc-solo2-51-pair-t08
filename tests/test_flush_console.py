"""Console routes for turbidity-driven flush endpoint control."""

from support import call, ok, refused

FLUSH_START = "/api/flush/start"
FLUSH_SAMPLE = "/api/flush/sample"
FLUSH_RELEASE = "/api/flush/release"
FLUSH_RUNS = "/api/flush/runs"
FLUSH_LIMITS = "/api/flush/limits"


def test_limits_report_a_baseline_revision(console):
    body = ok(console, "GET", FLUSH_LIMITS)
    assert body["current"]["version"] == 1
    assert body["current"]["cap_seconds"] == 90
    assert [row["version"] for row in body["revisions"]] == [1]


def test_full_clean_cycle_releases_the_flush(console):
    started = ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    assert started["created"] is True
    assert started["run"]["limits_version"] == 1

    verdict = ok(
        console,
        "POST",
        FLUSH_SAMPLE,
        {"run_id": "round-1", "request_id": "s1", "turbidity": 0.3, "residual": 0.2},
    )
    assert verdict["outcome"] == "release"
    assert verdict["basis"]["turbidity"]["value"] == 0.3

    released = ok(console, "POST", FLUSH_RELEASE, {"run_id": "round-1"})
    assert released["outcome"] == "release"


def test_release_while_running_is_refused(console):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    response = refused(console, "POST", FLUSH_RELEASE, {"run_id": "round-1"})
    assert response.status == 409
    assert "endpoint" in response.body["error"]


def test_dirty_samples_extend_then_trip_and_release_is_blocked(console):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    for i in range(3):
        body = {
            "run_id": "round-1",
            "request_id": f"d{i}",
            "turbidity": 4.0,
            "residual": 0.1,
        }
        verdict = ok(console, "POST", FLUSH_SAMPLE, body)
        assert verdict["outcome"] == "extend"
    trip = ok(
        console,
        "POST",
        FLUSH_SAMPLE,
        {"run_id": "round-1", "request_id": "d3", "turbidity": 4.0, "residual": 0.1},
    )
    assert trip["outcome"] == "exceeded"

    blocked = refused(console, "POST", FLUSH_RELEASE, {"run_id": "round-1"})
    assert blocked.status == 409
    assert "exceeded" in blocked.body["error"]


def test_missing_sample_is_not_a_release(console):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    verdict = ok(
        console,
        "POST",
        FLUSH_SAMPLE,
        {"run_id": "round-1", "request_id": "p1", "turbidity": 0.2},
    )
    assert verdict["outcome"] == "extend"
    assert verdict["reason"] == "missing or untrusted reading"


def test_untrusted_reading_is_not_a_release(console):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    verdict = ok(
        console,
        "POST",
        FLUSH_SAMPLE,
        {
            "run_id": "round-1",
            "request_id": "p2",
            "turbidity": {"value": 0.2, "trusted": False},
            "residual": 0.1,
        },
    )
    assert verdict["outcome"] == "extend"


def test_duplicate_sample_request_extends_once(console):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    body = {"run_id": "round-1", "request_id": "dup", "turbidity": 4.0, "residual": 0.1}
    first = ok(console, "POST", FLUSH_SAMPLE, body)
    second = ok(console, "POST", FLUSH_SAMPLE, body)
    assert first == second
    view = ok(console, "GET", f"{FLUSH_RUNS}?run_id=round-1")
    assert view["extensions_used"] == 1
    extend_events = [row for row in view["audit"] if row["kind"] == "flush.extended"]
    assert len(extend_events) == 1


def test_run_view_carries_the_audit_trail_with_basis_and_version(console):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    ok(
        console,
        "POST",
        FLUSH_SAMPLE,
        {"run_id": "round-1", "request_id": "d1", "turbidity": 4.0, "residual": 0.1},
    )
    view = ok(console, "GET", f"{FLUSH_RUNS}?run_id=round-1")
    kinds = [row["kind"] for row in view["audit"]]
    assert kinds == ["flush.started", "flush.extended"]
    extended = view["audit"][1]["payload"]
    assert extended["limits_version"] == 1
    assert extended["reason"] == "turbidity above limit"
    assert extended["basis"]["max_turbidity"] == 1.0


def test_running_round_uses_start_version_after_a_migration(console, plant):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    published_response = call(
        console,
        "POST",
        FLUSH_LIMITS,
        {
            "version": 2,
            "max_turbidity": 1.0,
            "max_residual": 0.5,
            "extend_seconds": 60,
            "max_extensions": 10,
        },
    )
    assert published_response.status == 201
    published = published_response.body
    assert published["version"] == 2
    view = ok(console, "GET", f"{FLUSH_RUNS}?run_id=round-1")
    assert view["limits_version"] == 1
    assert view["cap_seconds"] == 90

    ok(console, "POST", FLUSH_START, {"run_id": "round-2"})
    second = ok(console, "GET", f"{FLUSH_RUNS}?run_id=round-2")
    assert second["limits_version"] == 2
    assert second["cap_seconds"] == 600

    limits = ok(console, "GET", FLUSH_LIMITS)
    assert [row["version"] for row in limits["revisions"]] == [1, 2]


def test_migration_out_of_version_order_is_conflict(console):
    response = refused(
        console,
        "POST",
        FLUSH_LIMITS,
        {
            "version": 7,
            "max_turbidity": 1.0,
            "max_residual": 0.5,
            "extend_seconds": 30,
            "max_extensions": 2,
        },
    )
    assert response.status == 409


def test_malformed_limits_document_is_bad_request(console):
    response = call(
        console,
        "POST",
        FLUSH_LIMITS,
        {
            "version": 2,
            "max_turbidity": 0,
            "max_residual": 0.5,
            "extend_seconds": 30,
            "max_extensions": 2,
        },
    )
    assert response.status == 400


def test_sample_without_a_run_is_conflict(console):
    response = refused(
        console,
        "POST",
        FLUSH_SAMPLE,
        {"request_id": "s1", "turbidity": 0.1, "residual": 0.1},
    )
    assert response.status == 409


def test_runs_list_and_events_contain_flush_decisions(console):
    ok(console, "POST", FLUSH_START, {"run_id": "round-1"})
    ok(
        console,
        "POST",
        FLUSH_SAMPLE,
        {"run_id": "round-1", "request_id": "s1", "turbidity": 0.1, "residual": 0.1},
    )
    listing = ok(console, "GET", FLUSH_RUNS)
    assert len(listing["runs"]) == 1
    assert listing["runs"][0]["state"] == "released"

    events = ok(console, "GET", "/api/events")
    kinds = {event["kind"] for event in events["events"]}
    assert {"flush.started", "flush.released"} <= kinds


def test_flush_routes_are_registered(console):
    registered = {tuple(route) for route in console.routes()}
    for method, path in (
        ("POST", FLUSH_START),
        ("POST", FLUSH_SAMPLE),
        ("POST", FLUSH_RELEASE),
        ("GET", FLUSH_RUNS),
        ("GET", FLUSH_LIMITS),
        ("POST", FLUSH_LIMITS),
    ):
        assert (method, path) in registered
