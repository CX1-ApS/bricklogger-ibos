"""The iBOS Data API over HTTPS: a client paced under the instance's request
budget, with the API's answers turned into the errors the source acts on. See
``README.md``, "Fetching" and "Failed requests".
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx

from bricklogger_ibos.config import IBOSConfig

log = logging.getLogger(__name__)

TRENDING_LIMIT = 10_000
"""The most samples the API serves per request."""
PAGE_SIZE = 100
"""Projects per page, the API's maximum."""
DEFAULT_RETRY_AFTER = 1.0
MAX_RETRY_AFTER = 60.0
MAX_THROTTLE_RETRIES = 5


class APIError(Exception):
    """An answer the source cannot use; ``status`` is the HTTP status, 0 when none."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class Unauthorized(APIError):
    """401: the token is not accepted, so nothing can work."""


class Forbidden(APIError):
    """403: the token has no access to the project."""


class NotFound(APIError):
    """404: the object or project is not in iBOS."""


class Unavailable(APIError):
    """No usable answer: a server error, a timeout, no connection, or a 429 that
    does not clear."""


@dataclass
class Stats:
    """What the client has done, for status and tools."""

    requests: int = 0
    throttled: int = 0
    errors: int = 0


class Budget:
    """Evenly paced request starts under a rate, shared by all an instance does."""

    def __init__(self, rate: float) -> None:
        self.interval = 1.0 / rate
        self._next = 0.0
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        """Wait for the next slot; returns when the request may start."""
        async with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        delay = start - now
        if delay > 0:
            await asyncio.sleep(delay)


class IBOSClient:
    """The API's endpoints, every request under the budget."""

    def __init__(
        self,
        settings: IBOSConfig,
        budget: Budget | None = None,
        stats: Stats | None = None,
        page_size: int = TRENDING_LIMIT,
    ) -> None:
        self.settings = settings
        self.page_size = page_size
        self.budget = budget if budget is not None else Budget(settings.rate_limit)
        self.stats = stats if stats is not None else Stats()
        self._http = httpx.AsyncClient(
            base_url=settings.url,
            headers={
                "Authorization": f"Bearer {settings.token}",
                "Accept": "application/json",
            },
            timeout=settings.timeout.total_seconds(),
        )

    async def close(self) -> None:
        await self._http.aclose()

    async def get(self, path: str, params: Mapping[str, Any] | None = None) -> Any:
        """One GET under the budget; a 429 is waited out, other failures raise."""
        for _ in range(MAX_THROTTLE_RETRIES + 1):
            await self.budget.acquire()
            self.stats.requests += 1
            try:
                response = await self._http.get(path, params=dict(params or {}))
            except httpx.TimeoutException as exc:
                self.stats.errors += 1
                seconds = self.settings.timeout.total_seconds()
                raise Unavailable(0, f"no answer within {seconds:g}s") from exc
            except httpx.HTTPError as exc:
                self.stats.errors += 1
                raise Unavailable(0, f"no connection: {exc}") from exc
            status = response.status_code
            if status == 429:
                self.stats.throttled += 1
                delay = _retry_after(response)
                log.info("%s: rate limited by the API; waiting %.1fs", path, delay)
                await asyncio.sleep(delay)
                continue
            if status == 401:
                raise Unauthorized(status, "the token is not accepted (401)")
            if status == 403:
                raise Forbidden(status, "access denied (403)")
            if status == 404:
                raise NotFound(status, "not found (404)")
            if status >= 400:
                self.stats.errors += 1
                raise Unavailable(status, f"{status}: {_error_text(response)}")
            try:
                return response.json()
            except ValueError as exc:
                self.stats.errors += 1
                raise Unavailable(status, "the answer is not JSON") from exc
        self.stats.errors += 1
        raise Unavailable(429, "rate limited by the API without relief")

    # --- endpoints -----------------------------------------------------------

    async def projects(self) -> list[dict[str, Any]]:
        """Every project the token has access to, page by page."""
        items: list[dict[str, Any]] = []
        page = 1
        while True:
            data = await self.get(
                "/api/v1/projects", {"page": page, "page_size": PAGE_SIZE}
            )
            batch = _dicts(data, "projects")
            items.extend(batch)
            total = _int(data.get("total")) if isinstance(data, dict) else None
            if (
                not batch
                or len(batch) < PAGE_SIZE
                or (total is not None and len(items) >= total)
            ):
                return items
            page += 1

    async def project(self, ident: str) -> dict[str, Any]:
        return _dict(await self.get(f"/api/v1/projects/{ident}"))

    async def project_devices(self, ident: str) -> list[dict[str, Any]]:
        return _dicts(await self.get(f"/api/v1/projects/{ident}/devices"), "devices")

    async def device(self, device_uuid: str) -> dict[str, Any]:
        return _dict(await self.get(f"/api/v1/devices/{device_uuid}"))

    async def device_objects(self, device_uuid: str) -> list[dict[str, Any]]:
        return _dicts(
            await self.get(f"/api/v1/devices/{device_uuid}/objects"), "objects"
        )

    async def object(self, object_uuid: str) -> dict[str, Any]:
        return _dict(await self.get(f"/api/v1/objects/{object_uuid}"))

    async def trending(
        self,
        object_uuid: str,
        start: datetime | None,
        end: datetime | None,
        limit: int | None = None,
    ) -> dict[str, Any]:
        params: dict[str, Any] = {
            "limit": limit if limit is not None else self.page_size
        }
        if start is not None:
            params["start_time"] = iso(start)
        if end is not None:
            params["end_time"] = iso(end)
        return _dict(await self.get(f"/api/v1/objects/{object_uuid}/trending", params))


def iso(stamp: datetime) -> str:
    """A timestamp as the API writes it: UTC with a ``Z``."""
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _retry_after(response: httpx.Response) -> float:
    header = response.headers.get("Retry-After")
    try:
        seconds = float(header) if header is not None else DEFAULT_RETRY_AFTER
    except ValueError:
        seconds = DEFAULT_RETRY_AFTER
    return min(max(seconds, 0.0), MAX_RETRY_AFTER)


def _error_text(response: httpx.Response) -> str:
    try:
        data = response.json()
    except ValueError:
        return response.text.strip()[:200] or response.reason_phrase
    if isinstance(data, dict) and isinstance(data.get("error"), str):
        return str(data["error"])
    return response.reason_phrase or "error"


def _dict(data: Any) -> dict[str, Any]:
    return dict(data) if isinstance(data, dict) else {}


def _dicts(data: Any, key: str) -> list[dict[str, Any]]:
    items = data.get(key) if isinstance(data, dict) else None
    if not isinstance(items, list):
        return []
    return [dict(item) for item in items if isinstance(item, dict)]


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None
