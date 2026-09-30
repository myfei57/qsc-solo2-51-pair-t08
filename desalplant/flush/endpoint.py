"""Endpoint decision for one flush sample.

The flush is released only when both channels are trusted and within their
limits. A partial failure (missing or untrusted reading) or an out-of-limit
channel asks for an extension; once the pinned envelope is spent the same
decision trips the run instead, so flushing stops and is reported.
"""

from dataclasses import dataclass
from typing import Any

from .limits import FlushLimits
from .sample import RESIDUAL_CHANNEL, TURBIDITY_CHANNEL, FlushSample

RELEASE = "release"
EXTEND = "extend"
EXCEEDED = "exceeded"

REASON_CLEAN = "within limits"
REASON_TURBIDITY = "turbidity above limit"
REASON_RESIDUAL = "residual chemical above limit"
REASON_PARTIAL = "missing or untrusted reading"


@dataclass(frozen=True)
class Verdict:
    """What one sample means for the run, with the evidence behind it."""

    outcome: str
    reason: str
    sample: FlushSample
    limits: FlushLimits
    extensions_used: int

    def released(self) -> bool:
        """Whether the flush reached its endpoint."""
        return self.outcome == RELEASE

    def extension_granted(self) -> bool:
        """Whether another extension was granted."""
        return self.outcome == EXTEND

    def exceeded(self) -> bool:
        """Whether the extension envelope was spent without success."""
        return self.outcome == EXCEEDED

    def has_extension_room(self) -> bool:
        """Whether the pinned envelope still pays for another extension."""
        return self.extensions_used < self.limits.max_extensions

    def extensions_remaining(self) -> int:
        """Extensions the run can still be granted."""
        return max(0, self.limits.max_extensions - self.extensions_used)

    def extended_seconds(self) -> int:
        """Extension time already spent under the pinned envelope."""
        return self.extensions_used * self.limits.extend_seconds

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection used by audit rows and the console."""
        return {
            "outcome": self.outcome,
            "reason": self.reason,
            "sample": self.sample.to_dict(),
            "limits_version": self.limits.version,
            "extensions_used": self.extensions_used,
            "extensions_remaining": self.extensions_remaining(),
            "extended_seconds": self.extended_seconds(),
            "cap_seconds": self.limits.cap_seconds(),
            "basis": {
                TURBIDITY_CHANNEL: self.sample.turbidity.to_dict(),
                RESIDUAL_CHANNEL: self.sample.residual.to_dict(),
                "max_turbidity": self.limits.max_turbidity,
                "max_residual": self.limits.max_residual,
            },
        }


def judge(
    sample: FlushSample,
    limits: FlushLimits,
    extensions_used: int,
) -> Verdict:
    """Judge a sample against the pinned limits.

    Trust is checked first: a partial failure can never be a clean endpoint.
    The envelope is applied after the failure is established, so an unclean
    sample at the cap trips the run instead of extending it forever.
    """
    if not sample.complete():
        reason = REASON_PARTIAL
    elif sample.turbidity.value > limits.max_turbidity:
        reason = REASON_TURBIDITY
    elif sample.residual.value > limits.max_residual:
        reason = REASON_RESIDUAL
    else:
        return Verdict(
            outcome=RELEASE,
            reason=REASON_CLEAN,
            sample=sample,
            limits=limits,
            extensions_used=extensions_used,
        )
    outcome = EXTEND if extensions_used < limits.max_extensions else EXCEEDED
    return Verdict(
        outcome=outcome,
        reason=reason,
        sample=sample,
        limits=limits,
        extensions_used=extensions_used,
    )
