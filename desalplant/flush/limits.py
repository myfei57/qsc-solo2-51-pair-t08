"""Versioned endpoint limits: the flush thresholds and its extension ceiling."""

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from ..errors import InvalidLimitsError

if TYPE_CHECKING:  # pragma: no cover - import cycle guard for type checkers
    from .sample import FlushSample


@dataclass(frozen=True)
class EndpointLimits:
    """One generation of the endpoint thresholds and extension ceiling.

    ``max_seconds`` caps the total flush time. The ceiling is versioned policy:
    a running round is pinned to the generation it was started with, so a later
    migration never changes the rules that round must finish under.
    """

    version: int
    turbidity_limit: float
    residual_limit: float
    extend_seconds: int
    max_seconds: int

    def __post_init__(self) -> None:
        if self.version < 1:
            raise InvalidLimitsError("limits version starts at one")
        if self.turbidity_limit < 0 or self.residual_limit < 0:
            raise InvalidLimitsError("endpoint limits must not be negative")
        if self.extend_seconds <= 0:
            raise InvalidLimitsError("extend seconds must be positive")
        if self.max_seconds <= 0:
            raise InvalidLimitsError("max seconds must be positive")

    def clean(self, sample: "FlushSample") -> bool:
        """Whether a trusted sample shows the effluent is clean enough."""
        return (
            sample.turbidity <= self.turbidity_limit
            and sample.residual <= self.residual_limit
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection used by the console and audit."""
        return {
            "version": self.version,
            "turbidity_limit": self.turbidity_limit,
            "residual_limit": self.residual_limit,
            "extend_seconds": self.extend_seconds,
            "max_seconds": self.max_seconds,
        }
