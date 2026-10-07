"""Keep the discovery release's HTTP capabilities independent of unfinished workflows."""

from fastapi import FastAPI
from fastapi.routing import APIRoute

# An allowlist makes new consumer endpoints opt in to the first release. Account
# ownership, saved filters and erasure retain their existing auth/CSRF boundaries.
_DISCOVERY_ROUTES = {
    "/v1/me/muse/connection",
    "/v1/me/muse/batches",
    "/v1/muse/openapi.json",
    "/v1/muse/connector/batches",
    "/v1/muse/connector/batches/{batch_id}",
    "/v1/muse/connector/batches/{batch_id}/items/{event_id}/claim",
    "/v1/muse/connector/batches/{batch_id}/items/{event_id}/outcome",
    "/v1/ui-config",
    "/v1/onboard",
    "/v1/me",
    "/v1/me/profile",
    "/v1/preferences",
    "/v1/me/avatar",
    "/v1/me/erasure-requests",
    "/v1/me/saved-filters",
    "/v1/me/saved-filters/{saved_filter_id}",
    "/v1/me/saved-filters/{saved_filter_id}/applied",
    "/v1/catalog/events",
    "/v1/catalog/events/summary",
    "/v1/catalog/events/{canonical_event_id}",
    "/v1/catalog/entities",
    "/v1/catalog/entities/{entity_id}",
    "/v1/catalog/entities/{entity_id}/graph",
    "/v1/catalog/entity-directory",
    "/v1/catalog/entity-overview-graph",
    "/v1/catalog/entity-resolution",
}


def apply_release_profile(app: FastAPI, profile: str) -> None:
    """Remove deferred routes before serving or generating the OpenAPI schema.

    This also removes the legacy product shell: discovery is served by Next.js,
    whose navigation consumes the same server-owned /v1/ui-config profile.
    Operator and operational endpoints retain their own independent authorization.
    """
    if profile != "discovery":
        return
    app.router.routes[:] = [
        route
        for route in app.router.routes
        if not isinstance(route, APIRoute)
        or (
            route.path not in {"/", "/app"}
            and (not route.path.startswith("/v1/") or route.path in _DISCOVERY_ROUTES)
        )
    ]
    app.openapi_schema = None
