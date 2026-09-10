"""Read-only operator command investigation; structured evidence never exposes raw logs."""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response

from ..adapters.postgres.command_investigation import CommandInvestigationStore
from .admin import IngestionRunOut, _local_ingestion_admin

_NO_STORE = {"Cache-Control": "no-store, max-age=0"}


def install_command_investigation_routes(app: FastAPI) -> None:
    @app.get("/admin/v1/ingestion/commands/{command_id}/investigation", include_in_schema=False)
    async def command_investigation(
        command_id: UUID,
        request: Request,
        response: Response,
        authority: Annotated[object, Depends(_local_ingestion_admin)],
        after_event_id: Annotated[int, Query(ge=0, le=9223372036854775807)] = 0,
        limit: Annotated[int, Query(ge=1, le=200)] = 100,
        source_key: Annotated[str | None, Query(pattern=r"^[a-z0-9][a-z0-9-]{1,79}$")] = None,
    ) -> dict[str, Any]:
        del authority
        response.headers["Cache-Control"] = "no-store, max-age=0"
        store: CommandInvestigationStore | None = getattr(
            request.app.state, "command_investigation", None
        )
        if store is None:
            raise HTTPException(503, "command investigation unavailable", headers=_NO_STORE)
        try:
            result = await store.investigation(
                command_id, after_event_id=after_event_id, limit=limit, source_key=source_key
            )
            if result is not None:
                for task in result["plan"]["tasks"]:
                    if task["run"] is not None:
                        task["run"] = IngestionRunOut.model_validate(task["run"]).model_dump(
                            mode="json"
                        )
        except Exception as error:
            raise HTTPException(
                503, "command investigation unavailable", headers=_NO_STORE
            ) from error
        if result is None:
            raise HTTPException(404, "ingestion command not found", headers=_NO_STORE)
        return result
