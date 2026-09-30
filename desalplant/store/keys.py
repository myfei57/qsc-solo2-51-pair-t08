"""Key layout of the persisted control state."""

PLANT_STATE = "plant/state"
PLANT_STARTED_AT = "plant/started_at"
PLANT_STOPPED_AT = "plant/stopped_at"
PLANT_MAINTAINED_AT = "plant/maintained_at"

AUDIT_SEQUENCE = "audit/sequence"
AUDIT_EVENT_PREFIX = "audit/event/"
AUDIT_LAST_PREFIX = "audit/last/"

FLUSH_LIMITS_CURRENT = "flush/limits/current"
FLUSH_LIMIT_VERSION_PREFIX = "flush/limits/version/"
FLUSH_RUN_PREFIX = "flush/run/"


def audit_event_key(sequence: int) -> str:
    """Key holding the event written at ``sequence``."""
    if sequence < 1:
        raise ValueError("audit sequence starts at one")
    return f"{AUDIT_EVENT_PREFIX}{sequence:06d}"


def audit_last_key(kind: str) -> str:
    """Key holding the sequence of the latest event of ``kind``."""
    return f"{AUDIT_LAST_PREFIX}{kind}"


def flush_limit_key(version: int) -> str:
    """Key holding the flush limits document of one revision."""
    if version < 1:
        raise ValueError("flush limits versions start at one")
    return f"{FLUSH_LIMIT_VERSION_PREFIX}{version:06d}"


def flush_run_key(run_id: str) -> str:
    """Key holding the persisted snapshot of one flush run."""
    if not run_id:
        raise ValueError("flush run id must not be empty")
    return f"{FLUSH_RUN_PREFIX}{run_id}"
