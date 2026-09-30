"""One instrument reading with its trust classification.

A sample is only accepted when every channel is both present and trusted. A
missing sample or an untrusted reading is a partial failure: it never qualifies
the flush for release, it only triggers (or repeats) an extension.
"""

from dataclasses import dataclass
from typing import Any, Mapping

PRESENT = "present"
MISSING = "missing"
UNTRUSTED = "untrusted"


@dataclass(frozen=True)
class Reading:
    """A single channel reading and whether it can be judged."""

    channel: str
    value: float | None
    status: str = PRESENT

    @classmethod
    def missing(cls, channel: str) -> "Reading":
        """Build a reading for a channel that produced no sample."""
        return cls(channel=channel, value=None, status=MISSING)

    @classmethod
    def untrusted(cls, channel: str, value: float | None = None) -> "Reading":
        """Build a reading whose value cannot be trusted."""
        return cls(channel=channel, value=value, status=UNTRUSTED)

    @classmethod
    def from_mapping(cls, channel: str, row: Any) -> "Reading":
        """Build a reading from a console payload.

        ``None`` means the sample is missing. A mapping with ``trusted`` set to
        ``False`` keeps the value but marks it untrusted; a NaN value is treated
        the same way.
        """
        if row is None:
            return cls.missing(channel)
        if not isinstance(row, Mapping):
            value = float(row)
            return cls(channel=channel, value=value, status=PRESENT)
        if not bool(row.get("trusted", True)):
            raw = row.get("value")
            return cls.untrusted(channel, None if raw is None else float(raw))
        raw = row.get("value")
        if raw is None:
            return cls.missing(channel)
        value = float(raw)
        if value != value:  # NaN check without importing math
            return cls.untrusted(channel, None)
        return cls(channel=channel, value=value, status=PRESENT)

    def trusted(self) -> bool:
        """Whether the reading is good enough to judge the channel."""
        return self.status == PRESENT and self.value is not None

    def to_dict(self) -> dict[str, Any]:
        """JSON-friendly projection used by audit rows and the console."""
        return {"channel": self.channel, "value": self.value, "status": self.status}
