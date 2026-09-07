"""Operational metrics remain bounded and never label raw request targets."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any, cast

from events_concierge.infra.metrics import ApplicationMetrics, HttpMetricsMiddleware


def test_registry_renders_build_http_histogram_and_readiness_metrics() -> None:
    metrics = ApplicationMetrics(
        release_revision="abc123",
        image_digest="sha256:" + "a" * 64,
    )
    metrics.set_dependency_ready("database", True)
    metrics.set_dependency_ready("temporal", False)
    metrics.observe_http("GET", "/v1/tasks/{token}/done", 200, 0.02)

    rendered = metrics.render()

    assert 'release_revision="abc123"' in rendered
    assert 'dependency="database"} 1' in rendered
    assert 'dependency="temporal"} 0' in rendered
    assert (
        'events_concierge_http_requests_total{method="GET",route="/v1/tasks/{token}/done",status="200"} 1'
        in rendered
    )
    assert 'le="0.025"} 1' in rendered
    assert 'le="+Inf"} 1' in rendered


def test_registry_collapses_unknown_methods_and_untrusted_route_labels() -> None:
    metrics = ApplicationMetrics(release_revision="dev", image_digest=None)

    metrics.observe_http("ATTACK", "/literal/secret-token?token=plaintext", 999, -1.0)

    rendered = metrics.render()
    assert 'method="OTHER",route="unmatched",status="500"' in rendered
    assert "secret-token" not in rendered
    assert "plaintext" not in rendered


def test_registry_rejects_unbounded_dependency_labels() -> None:
    metrics = ApplicationMetrics(release_revision="dev", image_digest=None)

    try:
        metrics.set_dependency_ready("tenant-controlled", True)
    except ValueError as error:
        assert str(error) == "unknown readiness dependency"
    else:
        raise AssertionError("unknown dependency was accepted")


async def test_middleware_uses_the_router_template_after_dispatch() -> None:
    metrics = ApplicationMetrics(release_revision="dev", image_digest=None)

    async def app(scope: dict[str, Any], receive: object, send: object) -> None:
        del receive
        scope["route"] = type("Route", (), {"path": "/v1/tasks/{token}/done"})()
        sender = cast("Callable[[dict[str, Any]], Awaitable[None]]", send)
        await sender({"type": "http.response.start", "status": 410, "headers": []})
        await sender({"type": "http.response.body", "body": b""})

    sent: list[dict[str, Any]] = []

    async def receive() -> dict[str, str]:
        return {"type": "http.disconnect"}

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    middleware = HttpMetricsMiddleware(cast("Any", app), metrics=metrics)
    scope = cast(
        "Any",
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/tasks/raw-capability/done",
        },
    )
    await middleware(scope, cast("Any", receive), cast("Any", send))

    rendered = metrics.render()
    assert 'method="POST",route="/v1/tasks/{token}/done",status="410"' in rendered
    assert "raw-capability" not in rendered
    assert sent[0]["status"] == 410
