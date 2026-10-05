"""Read-only OpenRouter key totals; never return key labels, hashes or account identifiers."""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from ....domain.model_usage import reported_money


class OpenRouterKeyUsage:
    def __init__(self, api_key: str, *, client: httpx.AsyncClient | None = None) -> None:
        self._key = api_key
        self._client = client
        self._cached: dict[str, Any] | None = None
        self._expires = 0.0
        self._lock = asyncio.Lock()

    async def get(self) -> dict[str, Any]:
        if not self._key:
            return {"status": "not_configured", "checked_at": None}
        async with self._lock:
            if self._cached is not None and time.monotonic() < self._expires:
                return self._cached
            owns_client = self._client is None
            client = self._client or httpx.AsyncClient()
            try:
                response = await client.get(
                    "https://openrouter.ai/api/v1/key",
                    headers={"Authorization": f"Bearer {self._key}"},
                    timeout=5.0,
                )
                response.raise_for_status()
                data = response.json().get("data")
                if not isinstance(data, dict):
                    raise ValueError("invalid key metadata")
                result: dict[str, Any] = {
                    "status": "ok",
                    "checked_at": datetime.now(UTC).isoformat(),
                }
                for key in (
                    "usage",
                    "usage_daily",
                    "usage_weekly",
                    "usage_monthly",
                    "limit",
                    "limit_remaining",
                ):
                    amount = reported_money(data.get(key))
                    result[key] = str(amount) if amount is not None else None
                result["limit_state"] = (
                    "unlimited"
                    if "limit" in data and data["limit"] is None
                    else "configured"
                    if result["limit"] is not None
                    else "unknown"
                )
                reset = data.get("limit_reset")
                result["limit_reset"] = reset if reset in ("daily", "weekly", "monthly") else None
                self._cached = result
                self._expires = time.monotonic() + 60
                return result
            except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                # Failed reads do not turn a previously observed balance into a current one.
                self._cached = None
                return {"status": "unavailable", "checked_at": datetime.now(UTC).isoformat()}
            finally:
                if owns_client:
                    await client.aclose()
