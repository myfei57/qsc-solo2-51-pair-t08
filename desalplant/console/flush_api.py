"""Console handlers for turbidity-driven flush endpoint control."""

from ..flush.limits import FlushLimits
from ..flush.sample import FlushSample
from .http import Request, Response, json_response


def start(console, request: Request) -> Response:
    """Start a flush run pinned to the current limits revision."""
    body = request.body()
    run_id = body.get("run_id")
    return json_response(console.plant.start_flush_run(run_id or None))


def sample(console, request: Request) -> Response:
    """Judge one outlet sample: extend, release or trip the run."""
    body = request.body()
    run_id = body.pop("run_id", None)
    verdict = console.plant.evaluate_flush_sample(
        FlushSample.from_mapping(body), run_id or None
    )
    return json_response(verdict)


def release(console, request: Request) -> Response:
    """Release the flush only after the endpoint was reached."""
    body = request.body()
    return json_response(console.plant.release_flush(body.get("run_id") or None))


def runs(console, request: Request) -> Response:
    """Flush runs with their audit trail, newest first."""
    run_id = request.param("run_id")
    if run_id is not None:
        return json_response(console.plant.flush_run_view(run_id))
    return json_response({"runs": console.plant.flush_runs_list()})


def limits(console, request: Request) -> Response:
    """Current limits revision and the revision history."""
    if request.method == "POST":
        body = request.body()
        published = console.plant.publish_flush_limits(FlushLimits.from_dict(body))
        return json_response(published, status=201)
    return json_response(console.plant.flush_limits_view())
