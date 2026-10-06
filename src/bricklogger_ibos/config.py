"""The iBOS instance configuration.

``README.md`` defines the keys: ``token`` is required, ``url``
defaults to the production API, ``projects`` narrows the claim scope, and
``rate_limit``, ``backfill``, ``overlap`` and ``timeout`` shape the fetching.
"""

from __future__ import annotations

import re
import uuid
from datetime import timedelta
from typing import Annotated, Any

from bricklogger.sdk import (
    DURATION_HELP,
    Duration,
    format_duration,
    parse_duration,
)
from pydantic import (
    BaseModel,
    BeforeValidator,
    ConfigDict,
    Field,
    PlainSerializer,
    field_validator,
)

DEFAULT_URL = "https://api.data.ibostechnologies.com"
DEFAULT_RATE_LIMIT = 50.0
"""Requests per second: half of the API's documented 100 per token."""

PROJECT_HELP = "a project is its number or its UUID"
_URL = re.compile(r"^https?://[^\s/]+(?:/\S*)?$")


def _coerce_window(value: Any) -> Any:
    """A duration, or ``0`` for none."""
    if isinstance(value, timedelta):
        return value
    if isinstance(value, bool):
        raise ValueError(f"{DURATION_HELP}, or 0")
    if isinstance(value, int) and value == 0:
        return timedelta(0)
    if isinstance(value, str):
        if value.strip() == "0":
            return timedelta(0)
        return parse_duration(value)
    raise ValueError(f"{DURATION_HELP}, or 0")


Window = Annotated[
    timedelta,
    BeforeValidator(_coerce_window),
    PlainSerializer(format_duration, return_type=str, when_used="json"),
]
"""A duration that may also be written ``0``."""


def _coerce_project(value: Any) -> Any:
    if isinstance(value, bool):
        raise ValueError(PROJECT_HELP)
    if isinstance(value, int):
        if value < 0:
            raise ValueError("a project number is not negative")
        return value
    if isinstance(value, str):
        text = value.strip()
        if text.isdigit():
            return int(text)
        try:
            return str(uuid.UUID(text))
        except ValueError as exc:
            raise ValueError(PROJECT_HELP) from exc
    raise ValueError(PROJECT_HELP)


Project = Annotated[int | str, BeforeValidator(_coerce_project)]
"""A project written as its number, ``4711``, or as its UUID."""


class IBOSConfig(BaseModel):
    """One ``ibos`` instance in ``sources.yaml``."""

    model_config = ConfigDict(extra="forbid")

    token: str = Field(
        min_length=1,
        json_schema_extra={"format": "password"},
        description="The personal access token, given as ${VARIABLE} with "
        "the value in the env file",
    )
    url: str = Field(default=DEFAULT_URL, description="The API's base URL")
    projects: list[Project] = Field(
        default_factory=list,
        description="The projects the instance claims, by number or UUID; "
        "all when empty",
    )
    rate_limit: float = Field(
        default=DEFAULT_RATE_LIMIT,
        gt=0,
        description="The request budget in requests per second; the API "
        "allows 100 per token",
    )
    backfill: Window = Field(
        default=timedelta(days=7),
        description="How far back the first fetch of a point reaches; 0 for nothing",
    )
    overlap: Window = Field(
        default=timedelta(hours=1),
        description="How far behind the latest known sample every round starts again",
    )
    timeout: Duration = Field(
        default=timedelta(seconds=30),
        description="How long one request waits for its response",
    )

    @field_validator("url")
    @classmethod
    def _http_url(cls, value: str) -> str:
        text = value.strip().rstrip("/")
        if _URL.match(text) is None:
            raise ValueError(
                "the url is an http or https URL, e.g. "
                "https://api.data.ibostechnologies.com"
            )
        return text

    @property
    def restricted(self) -> bool:
        """Whether ``projects`` narrows the claim."""
        return bool(self.projects)

    def claims_project(self, project_id: int, project_uuid: str | None) -> bool:
        """Whether a project is in scope: unrestricted, or listed by number or UUID."""
        if not self.projects:
            return True
        return any(
            (isinstance(project, int) and project == project_id)
            or (isinstance(project, str) and project == project_uuid)
            for project in self.projects
        )
