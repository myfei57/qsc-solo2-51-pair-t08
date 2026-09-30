"""One effluent sample taken during an endpoint-guided flush."""

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlushSample:
    """Turbidity and residual-drug readings from the flush effluent.

    A sample the sensors could not certify is marked ``trusted=False``. It is a
    partial failure and must never be used to decide that the flush has reached
    its endpoint.
    """

    turbidity: float
    residual: float
    trusted: bool = True

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection used by the console and audit."""
        return {
            "turbidity": self.turbidity,
            "residual": self.residual,
            "trusted": self.trusted,
        }


def usable(sample: "FlushSample | None") -> bool:
    """Whether a sample is present and trusted enough to judge the endpoint."""
    return sample is not None and sample.trusted
