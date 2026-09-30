"""Operations that change the state of the line."""

from ..flush.sample import FlushSample
from .http import Request, Response, json_response


def start(console, request: Request) -> Response:
    """Bring the line up."""
    request.body()
    event = console.plant.start()
    return json_response({"status": "started", "event": event.to_dict()})


def shutdown(console, request: Request) -> Response:
    """Stop the line."""
    request.body()
    event = console.plant.shutdown()
    return json_response({"status": "stopped", "event": event.to_dict()})


def switch(console, request: Request) -> Response:
    """Move production to the standby tank."""
    request.body()
    event = console.plant.switch_tank()
    return json_response({"status": "switched", "event": event.to_dict()})


def maintain(console, request: Request) -> Response:
    """Run the maintenance routine."""
    request.body()
    event = console.plant.maintain()
    return json_response({"status": "maintained", "event": event.to_dict()})


def flush_start(console, request: Request) -> Response:
    """Start an endpoint-guided flush round."""
    request.body()
    run = console.plant.flush_start()
    return json_response({"status": "started", "run": run.to_dict()})


def flush_evaluate(console, request: Request) -> Response:
    """Evaluate one effluent sample against the pinned limits version."""
    body = request.body()
    sample = None
    if "turbidity" in body or "residual" in body:
        sample = FlushSample(
            turbidity=float(body["turbidity"]),
            residual=float(body["residual"]),
            trusted=bool(body.get("trusted", True)),
        )
    decision = console.plant.flush_evaluate(sample, body.get("key"))
    return json_response({"decision": decision.to_dict()})


def flush_migrate(console, request: Request) -> Response:
    """Adopt a new endpoint limits generation."""
    body = request.body()
    migrated = console.plant.flush_migrate(
        turbidity_limit=float(body["turbidity_limit"]),
        residual_limit=float(body["residual_limit"]),
        extend_seconds=int(body["extend_seconds"]),
        max_seconds=int(body["max_seconds"]),
    )
    return json_response({"limits": migrated.to_dict()})
