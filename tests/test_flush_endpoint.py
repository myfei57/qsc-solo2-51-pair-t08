"""Turbidity/residual endpoint judgement, versioned limits and readings."""

import pytest

from desalplant.flush.endpoint import (
    EXCEEDED,
    EXTEND,
    RELEASE,
    judge,
)
from desalplant.flush.limits import FlushLimits, revised
from desalplant.flush.reading import MISSING, PRESENT, UNTRUSTED, Reading
from desalplant.flush.sample import FlushSample

LIMITS = FlushLimits(
    max_turbidity=1.0,
    max_residual=0.5,
    extend_seconds=30,
    max_extensions=3,
    version=1,
)


def clean_sample(request_id: str = "s1") -> FlushSample:
    return FlushSample.from_mapping(
        {"request_id": request_id, "turbidity": 0.4, "residual": 0.2}
    )


def dirty_sample(request_id: str = "s1") -> FlushSample:
    return FlushSample.from_mapping(
        {"request_id": request_id, "turbidity": 2.5, "residual": 0.2}
    )


def test_clean_sample_releases_within_both_limits():
    verdict = judge(clean_sample(), LIMITS, 0)
    assert verdict.outcome == RELEASE
    assert verdict.released() is True
    assert verdict.reason == "within limits"


def test_turbidity_above_limit_asks_for_an_extension():
    verdict = judge(dirty_sample(), LIMITS, 0)
    assert verdict.outcome == EXTEND
    assert verdict.extension_granted() is True
    assert verdict.reason == "turbidity above limit"
    assert verdict.has_extension_room() is True


def test_residual_above_limit_asks_for_an_extension():
    sample = FlushSample.from_mapping(
        {"request_id": "s", "turbidity": 0.3, "residual": 9.0}
    )
    verdict = judge(sample, LIMITS, 1)
    assert verdict.outcome == EXTEND
    assert verdict.reason == "residual chemical above limit"
    assert verdict.extensions_used == 1
    assert verdict.extensions_remaining() == 2


def test_missing_sample_is_a_partial_failure_not_a_release():
    sample = FlushSample.from_mapping({"request_id": "s", "turbidity": 0.4})
    assert sample.complete() is False
    assert sample.missing_channels() == ("residual",)
    verdict = judge(sample, LIMITS, 0)
    assert verdict.outcome == EXTEND
    assert verdict.reason == "missing or untrusted reading"


def test_untrusted_reading_is_a_partial_failure_not_a_release():
    sample = FlushSample.from_mapping(
        {"request_id": "s", "turbidity": {"value": 0.4, "trusted": False}, "residual": 0.1}
    )
    assert sample.complete() is False
    assert sample.untrusted_channels() == ("turbidity",)
    verdict = judge(sample, LIMITS, 0)
    assert verdict.outcome == EXTEND
    assert verdict.reason == "missing or untrusted reading"


def test_clean_readings_on_both_channels_never_partial_fail():
    reading = Reading.from_mapping("turbidity", {"value": 0.9, "trusted": True})
    assert reading.status == PRESENT
    assert reading.trusted() is True
    assert Reading.missing("x").status == MISSING
    assert Reading.untrusted("x").status == UNTRUSTED


def test_partial_failure_at_the_cap_trips_the_run():
    sample = FlushSample.from_mapping({"request_id": "s", "turbidity": 0.4})
    verdict = judge(sample, LIMITS, LIMITS.max_extensions)
    assert verdict.outcome == EXCEEDED
    assert verdict.exceeded() is True


def test_dirty_sample_at_the_cap_trips_instead_of_extending_forever():
    verdict = judge(dirty_sample(), LIMITS, LIMITS.max_extensions)
    assert verdict.outcome == EXCEEDED
    assert verdict.extensions_remaining() == 0
    assert verdict.extended_seconds() == LIMITS.cap_seconds()


def test_verdict_carries_the_judgement_basis():
    verdict = judge(dirty_sample("s9"), LIMITS, 2)
    row = verdict.to_dict()
    assert row["limits_version"] == 1
    assert row["extensions_used"] == 2
    assert row["extensions_remaining"] == 1
    assert row["cap_seconds"] == 90
    assert row["basis"]["turbidity"]["value"] == 2.5
    assert row["basis"]["max_turbidity"] == 1.0
    assert row["basis"]["max_residual"] == 0.5
    assert row["sample"]["request_id"] == "s9"


def test_limits_revision_is_immutable_and_versioned():
    assert LIMITS.cap_seconds() == 90
    next_revision = revised(LIMITS, max_extensions=5)
    assert next_revision.version == 2
    assert next_revision.max_extensions == 5
    assert next_revision.extend_seconds == 30
    assert LIMITS.version == 1
    with pytest.raises(ValueError):
        FlushLimits(max_turbidity=0, max_residual=1, extend_seconds=1, max_extensions=1)
    with pytest.raises(ValueError):
        FlushLimits(max_turbidity=1, max_residual=1, extend_seconds=0, max_extensions=1)


def test_limits_round_trip():
    assert FlushLimits.from_dict(LIMITS.to_dict()) == LIMITS
