"""One paired turbidity and residual sample taken from the flush outlet."""

from dataclasses import dataclass
from typing import Any, Mapping

from .reading import Reading

TURBIDITY_CHANNEL = "turbidity"
RESIDUAL_CHANNEL = "residual"


@dataclass(frozen=True)
class FlushSample:
    """The two channels judged together at one sampling point."""

    turbidity: Reading
    residual: Reading
    sample_id: str = ""
    request_id: str = ""

    @classmethod
    def from_mapping(cls, row: Mapping[str, Any]) -> "FlushSample":
        """Build a sample from a console payload."""
        sample_id = str(row.get("sample_id", "") or "")
        request_id = str(row.get("request_id", "") or sample_id)
        return cls(
            turbidity=Reading.from_mapping(TURBIDITY_CHANNEL, row.get("turbidity")),
            residual=Reading.from_mapping(RESIDUAL_CHANNEL, row.get("residual")),
            sample_id=sample_id,
            request_id=request_id,
        )

    def complete(self) -> bool:
        """Whether every channel produced a trusted reading."""
        return self.turbidity.trusted() and self.residual.trusted()

    def missing_channels(self) -> tuple[str, ...]:
        """Channels that did not produce a sample."""
        return tuple(
            reading.channel
            for reading in (self.turbidity, self.residual)
            if reading.status == "missing"
        )

    def untrusted_channels(self) -> tuple[str, ...]:
        """Channels whose reading could not be trusted."""
        return tuple(
            reading.channel
            for reading in (self.turbidity, self.residual)
            if reading.status == "untrusted"
        )

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection used by audit rows and the console."""
        return {
            "sample_id": self.sample_id,
            "request_id": self.request_id,
            "turbidity": self.turbidity.to_dict(),
            "residual": self.residual.to_dict(),
        }
