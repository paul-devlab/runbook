"""Prometheus metrics.

Prometheus *scrapes* (fetches) the app's current numbers every 30 seconds
from http://<pod>:9000/metrics and stores them over time; Grafana draws graphs
from them (Phase 4, later PRs).

What we count:
- runbook_http_requests_total{method, route, status}: every request.
- runbook_http_request_duration_seconds{method, route}: how long they take.
- runbook_ask_questions_total{outcome}: Ask questions, by what happened.
- runbook_ask_tokens_total{model, direction}: tokens sent to / received from Claude.
- runbook_ask_cost_dollars_total{model}: the *estimated* cost of those tokens
  (the Anthropic Console has the real bill).
- Plus the library's built-in process metrics: memory, CPU, open files.

Labels (the {...} parts) must come from a small, fixed set of values: each
combination is stored as its own series. So `route` is the route *template*
("/runbooks/{runbook_id}"), never the real path with an id in it.

Metrics are served on their own port (METRICS_PORT, default 9000), separate
from the website on 8000. The Ingress only routes to 8000, so the numbers are
never reachable from outside the cluster.
"""

import logging
import time

from fastapi import Request
from prometheus_client import Counter, Histogram, start_http_server
from starlette.responses import Response

logger = logging.getLogger(__name__)

HTTP_REQUESTS = Counter(
    "runbook_http_requests",
    "HTTP requests handled, by route template and status code.",
    ["method", "route", "status"],
)
HTTP_DURATION = Histogram(
    "runbook_http_request_duration_seconds",
    "Time to handle a request, in seconds.",
    ["method", "route"],
)
ASK_QUESTIONS = Counter(
    "runbook_ask_questions",
    "Questions submitted to Ask, by outcome.",
    ["outcome"],
)
ASK_TOKENS = Counter(
    "runbook_ask_tokens",
    "Tokens sent to (input) and received from (output, incl. thinking) Claude.",
    ["model", "direction"],
)
ASK_COST = Counter(
    "runbook_ask_cost_dollars",
    "Estimated cost of Ask's Claude calls, in US dollars.",
    ["model"],
)

# Every outcome Ask can have (app/ask.py). Listed so each shows on graphs as 0
# from the start, instead of appearing only after it first happens.
ASK_OUTCOMES = (
    "answered",  # Claude answered
    "cut_short",  # answered, but hit max_tokens
    "nothing_found",  # no matching records; Claude not called
    "rate_limited",  # over the per-user limit; Claude not called
    "refused",  # Claude declined
    "spend_limit",  # Anthropic refused: monthly spend limit reached
    "error",  # any other API problem
)
for _outcome in ASK_OUTCOMES:
    ASK_QUESTIONS.labels(_outcome)

# US dollars per million tokens: (input, output). From Anthropic's price list;
# update when prices or models change.
PRICES = {
    "claude-opus-5-5": (4.00, 20.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-haiku-5-5": (0.10, 0.50),
}
# A model not in the table is costed at the most expensive known price, so the
# estimate errs high rather than hiding spending.
_HIGHEST = max(PRICES.values())


def estimated_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    input_price, output_price = PRICES.get(model, _HIGHEST)
    return (input_tokens * input_price + output_tokens * output_price) / 1_000_000


def record_ask_usage(model: str, input_tokens: int, output_tokens: int) -> None:
    ASK_TOKENS.labels(model, "input").inc(input_tokens)
    ASK_TOKENS.labels(model, "output").inc(output_tokens)
    ASK_COST.labels(model).inc(estimated_cost(model, input_tokens, output_tokens))


def route_label(request: Request) -> str:
    """The route template that handled the request, e.g. "/runbooks/{runbook_id}"."""
    route = request.scope.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    if request.url.path.startswith("/static/"):
        return "/static"
    return "unmatched"  # a 404 for a made-up URL: don't keep the URL itself


async def metrics_middleware(request: Request, call_next) -> Response:
    start = time.perf_counter()
    status = 500
    try:
        response = await call_next(request)
        status = response.status_code
        return response
    finally:
        route = route_label(request)  # known only after routing, so read it here
        HTTP_REQUESTS.labels(request.method, route, str(status)).inc()
        HTTP_DURATION.labels(request.method, route).observe(time.perf_counter() - start)


def start_metrics_server(port: int):
    """Serve /metrics on its own port, in a background thread. Returns the
    server (to shut down later), or None if port is 0 (metrics turned off)."""
    if not port:
        return None
    server, _thread = start_http_server(port)
    logger.info("Serving metrics", extra={"port": port})
    return server
