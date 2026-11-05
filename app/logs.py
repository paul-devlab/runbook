"""Structured (JSON) logging.

Every log line is one JSON object, e.g.

    {"time": "2026-11-05T16:02:11.482+00:00", "level": "INFO", "logger": "app.requests",
     "msg": "GET /runbooks 200", "request_id": "6f1c0b9e2a7d4c53",
     "method": "GET", "path": "/runbooks", "status": 200, "duration_ms": 12.4}

Why JSON: log tools (and `kubectl logs ... | jq`) can filter on fields, e.g.
"every request with status >= 500", without fragile text matching.

How it works:
- Python's standard `logging` module is unchanged for the code that logs:
  `logger.info("...")` as usual. Only the *output format* changes, via
  JsonFormatter, set up once by `setup_logging()` when the app starts.
- Extra fields go in `extra=`: `logger.info("msg", extra={"status": 200})`.
- `request_log_middleware` gives each request a short random id, writes one
  line per request, and returns the id in the `X-Request-ID` header. Every
  line logged while handling that request carries the same `request_id`, so
  you can find all lines for one request.

Privacy: lines never include query strings, form data, cookies or headers
(search terms or Ask questions could be sensitive; Ask questions are never
logged, docs/SPEC.md §6).
"""

import json
import logging
import secrets
import sys
import time
from contextvars import ContextVar
from datetime import UTC, datetime

from fastapi import Request
from starlette.responses import Response

REQUEST_ID_HEADER = "X-Request-ID"

# The current request's id. A ContextVar is like a global variable, but each
# request (even when many run at once) sees its own value.
request_id: ContextVar[str] = ContextVar("request_id", default="")

# Attributes every LogRecord has; anything else on a record came from extra=.
# (Uvicorn adds "color_message", a copy with terminal colour codes: not wanted.)
_STANDARD = set(vars(logging.makeLogRecord({}))) | {
    "message",
    "asctime",
    "taskName",
    "color_message",
}

# Probes Kubernetes makes every few seconds: only logged when they fail.
_QUIET_PATHS = {"/health", "/ready"}

logger = logging.getLogger("app.requests")


class JsonFormatter(logging.Formatter):
    """Formats a log record as one line of JSON."""

    def format(self, record: logging.LogRecord) -> str:
        line = {
            "time": datetime.fromtimestamp(record.created, UTC).isoformat(timespec="milliseconds"),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        for key, value in vars(record).items():
            if key not in _STANDARD and not key.startswith("_"):
                line[key] = value
        if record.exc_info:
            line["error"] = self.formatException(record.exc_info)
        # default=str: anything that isn't JSON (a datetime, say) is written as text.
        return json.dumps(line, default=str)


def _add_request_id(factory):
    """Wrap Python's LogRecord factory so every record made during a request
    carries `request_id` (then JsonFormatter writes it like any extra field)."""

    def make_record(*args, **kwargs) -> logging.LogRecord:
        record = factory(*args, **kwargs)
        if rid := request_id.get():
            record.request_id = rid
        return record

    make_record.adds_request_id = True
    return make_record


def setup_logging(level: int = logging.INFO) -> None:
    """Send all logs, ours and Uvicorn's, to stdout as JSON.

    Containers should log to stdout: Kubernetes collects it (`kubectl logs`).
    """
    factory = logging.getLogRecordFactory()
    if not getattr(factory, "adds_request_id", False):  # only wrap it once
        logging.setLogRecordFactory(_add_request_id(factory))
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(JsonFormatter())
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)
    # Uvicorn sets up its own text handlers; hand its messages to ours instead.
    for name in ("uvicorn", "uvicorn.error"):
        uvicorn_logger = logging.getLogger(name)
        uvicorn_logger.handlers = []
        uvicorn_logger.propagate = True
    # Uvicorn's access log ("GET / 200") is replaced by request_log_middleware,
    # which adds the request id and duration.
    logging.getLogger("uvicorn.access").disabled = True


async def request_log_middleware(request: Request, call_next) -> Response:
    """Give the request an id, then log one line when it's done."""
    rid = secrets.token_hex(8)  # 16 hex characters: unique enough to search for
    token = request_id.set(rid)
    start = time.perf_counter()
    status = 500  # if the app crashes, there's no response to read it from
    try:
        response = await call_next(request)
        status = response.status_code
        response.headers[REQUEST_ID_HEADER] = rid
        return response
    finally:
        path = request.url.path  # no query string (see "Privacy" above)
        if not (path in _QUIET_PATHS and status < 400):
            logger.log(
                logging.ERROR if status >= 500 else logging.INFO,
                "%s %s %s",
                request.method,
                path,
                status,
                extra={
                    "method": request.method,
                    "path": path,
                    "status": status,
                    "duration_ms": round((time.perf_counter() - start) * 1000, 1),
                },
            )
        request_id.reset(token)
