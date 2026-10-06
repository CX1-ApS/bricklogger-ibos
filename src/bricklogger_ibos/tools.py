"""The iBOS protocol tools: ``projects``, ``devices``, ``objects``, ``read``,
``resolve`` and ``pointlist``. They run inside the source's loop when the daemon
runs it, and on a client of their own otherwise. See
``README.md``, "Protocol tools".
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

from bricklogger.sdk import Duration, UnknownPrefix, expand
from bricklogger.sdk.contract import GraphReader, ValueType
from pydantic import BaseModel, ConfigDict, Field

from bricklogger_ibos.client import TRENDING_LIMIT, APIError, IBOSClient
from bricklogger_ibos.config import IBOSConfig
from bricklogger_ibos.references import resolve_references
from bricklogger_ibos.values import (
    Sample,
    convert,
    newest_first,
    normalise_type,
    samples_of,
    unit_for,
    value_type_for,
)

TOOL_NAMES = ("projects", "devices", "objects", "read", "resolve", "pointlist")
LATEST_WINDOW = timedelta(hours=24)
"""How far back the latest sample of an object is looked for."""
TOOL_IN_FLIGHT = 8
"""Requests in the air at once when a tool asks per object; the budget paces them."""


class ToolError(RuntimeError):
    """The tool cannot complete; the message is for the operator."""


class ProjectsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DevicesParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str = Field(description="The project: its number or its UUID.")


class ObjectsParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    device: str = Field(description="The device's UUID.")
    values: bool = Field(
        default=False, description="Add the latest sample of every object."
    )


class ReadParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    object: str = Field(description="The object's UUID.")
    since: Duration = Field(
        default=timedelta(hours=1), description="How far back to read, e.g. 1h."
    )
    limit: int = Field(
        default=100,
        ge=1,
        le=TRENDING_LIMIT,
        description="The most samples to show, the latest first.",
    )


class ResolveParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    point: str = Field(description="The point URI, full or prefixed.")


class PointlistParameters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project: str | None = Field(
        default=None,
        description="One project, its number or its UUID; default every project "
        "the instance claims.",
    )


def timeout_for(name: str, parameters: Mapping[str, Any]) -> float:
    """How long the caller waits for the tool; ``objects --values`` asks per
    object, and ``pointlist`` per project and device."""
    return 300.0 if name in ("objects", "pointlist") else 60.0


async def run(
    name: str,
    client: IBOSClient,
    settings: IBOSConfig,
    graph: GraphReader | None,
    parameters: Mapping[str, Any],
    instance: str | None = None,
) -> Any:
    """Run one tool by name; raises ToolError when it cannot complete."""
    try:
        if name == "projects":
            ProjectsParameters.model_validate(parameters)
            return await projects(client)
        if name == "devices":
            return await devices(client, DevicesParameters.model_validate(parameters))
        if name == "objects":
            return await objects(client, ObjectsParameters.model_validate(parameters))
        if name == "read":
            return await read(client, ReadParameters.model_validate(parameters))
        if name == "pointlist":
            return await pointlist(
                client,
                settings,
                instance,
                PointlistParameters.model_validate(parameters),
            )
    except APIError as exc:
        raise ToolError(f"the API answered: {exc.message}") from exc
    if name == "resolve":
        return await resolve(
            client, settings, graph, ResolveParameters.model_validate(parameters)
        )
    raise ToolError(f"the iBOS source has no tool {name!r}")


# --- the tools ---------------------------------------------------------------


def describe_project(item: Mapping[str, Any]) -> dict[str, Any]:
    """A project as the tools show it: number, UUID, name and description."""
    return {
        "number": item.get("project_id"),
        "uuid": item.get("id"),
        "name": item.get("project_name"),
        "description": item.get("project_description"),
    }


def describe_device(item: Mapping[str, Any]) -> dict[str, Any]:
    """A device as the tools show it: UUID, number, name, vendor, model and IP."""
    return {
        "uuid": item.get("id"),
        "number": item.get("device_id"),
        "name": item.get("device_name"),
        "vendor": item.get("vendor_name"),
        "model": item.get("model_name"),
        "ip": item.get("ip_address"),
    }


def describe_object(item: Mapping[str, Any]) -> dict[str, Any]:
    """An object as the tools show it, the type in the standard's spelling."""
    return {
        "uuid": item.get("id"),
        "type": normalise_type(item.get("object_type")),
        "instance": item.get("object_instance"),
        "name": item.get("object_name"),
        "unit": item.get("units"),
        "qudt": unit_for(item.get("unit_id"), item.get("units")),
    }


async def projects(client: IBOSClient) -> list[dict[str, Any]]:
    return [
        {
            **describe_project(item),
            "devices": item.get("device_count"),
            "objects": item.get("object_count"),
        }
        for item in await client.projects()
    ]


async def devices(client: IBOSClient, p: DevicesParameters) -> list[dict[str, Any]]:
    return [
        describe_device(item)
        for item in await client.project_devices(p.project.strip())
    ]


async def objects(client: IBOSClient, p: ObjectsParameters) -> list[dict[str, Any]]:
    rows = [
        describe_object(item) for item in await client.device_objects(p.device.strip())
    ]
    if p.values:
        semaphore = asyncio.Semaphore(TOOL_IN_FLIGHT)

        async def fill(row: dict[str, Any]) -> None:
            async with semaphore:
                row.update(
                    await latest_sample(
                        client, row["uuid"], value_type_for(row["type"])
                    )
                )

        await asyncio.gather(
            *(fill(row) for row in rows if isinstance(row["uuid"], str))
        )
    return rows


async def pointlist(
    client: IBOSClient,
    settings: IBOSConfig,
    instance: str | None,
    p: PointlistParameters,
) -> dict[str, Any]:
    """The point list: the projects the instance claims, or the one project,
    with their devices and the objects on each, as one document. One request
    per project and one per device; no samples."""
    if p.project is not None:
        found = [await client.project(p.project.strip())]
    else:
        found = []
        for item in await client.projects():
            number, uuid = item.get("project_id"), item.get("id")
            if settings.claims_project(
                number if isinstance(number, int) else -1,
                uuid if isinstance(uuid, str) else None,
            ):
                found.append(item)
    semaphore = asyncio.Semaphore(TOOL_IN_FLIGHT)

    async def with_objects(device: Mapping[str, Any]) -> dict[str, Any]:
        described = describe_device(device)
        uuid = device.get("id")
        async with semaphore:
            items = await client.device_objects(uuid) if isinstance(uuid, str) else []
        described["objects"] = [describe_object(item) for item in items]
        return described

    entries: list[dict[str, Any]] = []
    for item in found:
        entry = describe_project(item)
        ident = entry["number"] if entry["number"] is not None else entry["uuid"]
        devices = await client.project_devices(str(ident))
        entry["devices"] = list(
            await asyncio.gather(*(with_objects(device) for device in devices))
        )
        entries.append(entry)
    return {
        "instance": instance,
        "url": settings.url,
        "exported_at": datetime.now(UTC).replace(microsecond=0).isoformat(),
        "counts": {
            "projects": len(entries),
            "devices": sum(len(entry["devices"]) for entry in entries),
            "objects": sum(
                len(device["objects"])
                for entry in entries
                for device in entry["devices"]
            ),
        },
        "projects": entries,
    }


async def read(client: IBOSClient, p: ReadParameters) -> list[dict[str, Any]]:
    """The object's latest samples, the latest first, as the vocabulary sees them."""
    object_uuid = p.object.strip()
    metadata = await client.object(object_uuid)
    value_type = value_type_for(normalise_type(metadata.get("object_type")))
    now = datetime.now(UTC)
    data = await client.trending(object_uuid, now - p.since, now)
    samples = samples_of(data)[-p.limit :]
    return [describe_sample(sample, value_type) for sample in reversed(samples)]


async def resolve(
    client: IBOSClient,
    settings: IBOSConfig,
    graph: GraphReader | None,
    p: ResolveParameters,
) -> dict[str, Any]:
    if graph is None:
        raise ToolError("resolve needs the running daemon's working graph")
    try:
        uri = expand(p.point, graph.prefixes)
    except UnknownPrefix:
        uri = p.point
    resolved = resolve_references(graph)
    result: dict[str, Any] = {"point": uri}
    reference = resolved.references.get(uri)
    if reference is None:
        problem = resolved.problems.get(uri)
        result["problem"] = (
            problem.reason if problem is not None else "the point has no iBOS reference"
        )
        if problem is not None:
            result["project"] = problem.project_id
        return result
    result.update(
        {
            "object": reference.object_uuid,
            "project": reference.project_id,
            "project_uuid": reference.project_uuid,
            "in_scope": settings.claims_project(
                reference.project_id, reference.project_uuid
            ),
        }
    )
    try:
        metadata = await client.object(reference.object_uuid)
    except APIError as exc:
        result["error"] = exc.message
        return result
    api_type = normalise_type(metadata.get("object_type"))
    api_instance = metadata.get("object_instance")
    result.update(
        {
            "name": metadata.get("object_name"),
            "type": api_type,
            "instance": api_instance,
            "unit": metadata.get("units"),
            "qudt": unit_for(metadata.get("unit_id"), metadata.get("units")),
        }
    )
    checks: list[str] = []
    if (
        reference.object_type is not None
        and api_type is not None
        and reference.object_type != api_type
    ):
        checks.append(f"the graph says object type {reference.object_type!r}")
    if (
        reference.object_instance is not None
        and isinstance(api_instance, int)
        and not isinstance(api_instance, bool)
        and reference.object_instance != api_instance
    ):
        checks.append(f"the graph says object instance {reference.object_instance}")
    result["check"] = "; ".join(checks) if checks else "ok"
    value_type = value_type_for(api_type)
    result["value_type"] = value_type
    result.update(await latest_sample(client, reference.object_uuid, value_type))
    return result


# --- samples -----------------------------------------------------------------


async def latest_sample(
    client: IBOSClient, object_uuid: str, value_type: ValueType | None
) -> dict[str, Any]:
    """The object's latest sample within the window, or why there is none.

    The API serves the newest first, so two samples settle it; should an
    answer come the other way round, the whole window is read.
    """
    now = datetime.now(UTC)
    try:
        data = await client.trending(object_uuid, now - LATEST_WINDOW, now, limit=2)
        if newest_first(data) is False:
            data = await client.trending(object_uuid, now - LATEST_WINDOW, now)
    except APIError as exc:
        return {"error": exc.message}
    samples = samples_of(data)
    if not samples:
        return {"timestamp": None, "value": None}
    described = describe_sample(samples[-1], value_type)
    return {key: value for key, value in described.items() if key != "type"}


def describe_sample(sample: Sample, value_type: ValueType | None) -> dict[str, Any]:
    """A sample as the vocabulary sees it, with the API's raw fields beside."""
    data: dict[str, Any] = {
        "timestamp": sample.timestamp.isoformat(),
        "type": None,
        "value": None,
        "raw_value": sample.value,
        "value_text": sample.text,
    }
    if value_type is None:
        return data
    converted = convert(value_type, sample.value, sample.text)
    data["type"] = converted.type
    if converted.type == "null":
        data["reason"] = converted.reason
    elif isinstance(converted.value, datetime):
        data["value"] = converted.value.isoformat()
    else:
        data["value"] = converted.value
    return data
