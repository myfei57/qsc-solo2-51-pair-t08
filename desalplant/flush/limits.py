"""Versioned configuration of the flush extension envelope.

A revision of the limits is immutable once published. A run pins the revision
that is current when the run starts, so a configuration migration never changes
the envelope of a run that is already in flight.
"""

from dataclasses import dataclass
from typing import Any, Mapping

CURRENT_VERSION_KEY = "version"


@dataclass(frozen=True)
class FlushLimits:
    """Acceptance thresholds and the extension envelope for one revision."""

    max_turbidity: float
    max_residual: float
    extend_seconds: int
    max_extensions: int
    version: int = 1

    def __post_init__(self) -> None:
        if self.max_turbidity <= 0:
            raise ValueError("max turbidity must be positive")
        if self.max_residual <= 0:
            raise ValueError("max residual must be positive")
        if self.extend_seconds <= 0:
            raise ValueError("extension seconds must be positive")
        if self.max_extensions <= 0:
            raise ValueError("max extensions must be positive")
        if self.version < 1:
            raise ValueError("limits version starts at one")

    def cap_seconds(self) -> int:
        """Whole extension budget attached to this revision."""
        return self.extend_seconds * self.max_extensions

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection used by the store and the console."""
        return {
            "version": self.version,
            "max_turbidity": self.max_turbidity,
            "max_residual": self.max_residual,
            "extend_seconds": self.extend_seconds,
            "max_extensions": self.max_extensions,
            "cap_seconds": self.cap_seconds(),
        }

    @classmethod
    def from_dict(cls, row: Mapping[str, Any]) -> "FlushLimits":
        """Rebuild a revision from a persisted row."""
        return cls(
            version=int(row["version"]),
            max_turbidity=float(row["max_turbidity"]),
            max_residual=float(row["max_residual"]),
            extend_seconds=int(row["extend_seconds"]),
            max_extensions=int(row["max_extensions"]),
        )


def revised(current: FlushLimits, **values: Any) -> FlushLimits:
    """Publish the next revision, bumping the version automatically."""
    next_version = current.version + 1
    fields = {
        "max_turbidity": current.max_turbidity,
        "max_residual": current.max_residual,
        "extend_seconds": current.extend_seconds,
        "max_extensions": current.max_extensions,
    }
    fields.update(values)
    return FlushLimits(version=next_version, **fields)
