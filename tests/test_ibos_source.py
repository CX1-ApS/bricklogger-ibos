"""The iBOS source against a fake cloud on localhost: fetching with the cloud's
timestamps, backfill and overlap, values and learned texts, the API's answers,
the request budget, and the tools."""

from __future__ import annotations

import asyncio
import functools
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any, cast

import pytest
import uvicorn
from bricklogger.sdk.contract import (
    AssignedPoint,
    GraphReader,
    Outcome,
    PointMetadata,
)
from bricklogger.sdk.declaration import SourceDeclaration, ToolDeclaration, ToolOffer
from bricklogger.sdk.testing import Collector, graph_from_turtle
from bricklogger.sdk.testing import assigned as assigned_point
from fastapi import FastAPI, Header, HTTPException, Query
from fastapi.responses import JSONResponse
from pydantic import ValidationError

from bricklogger_ibos.client import Unauthorized
from bricklogger_ibos.config import IBOSConfig
from bricklogger_ibos.declaration import SOURCE
from bricklogger_ibos.source import FetchPoint, IBOSSource, ProjectFetcher
from bricklogger_ibos.tools import ProjectsParameters, ToolError
from bricklogger_ibos.values import (
    convert,
    normalise_type,
    unit_for,
    value_type_for,
)
from tests.support import free_port, wait_for

EX = "https://example.com/bldg#"
QUDT = "http://qudt.org/vocab/unit/"
TOKEN = "ibos_pat_test"
PROJECT_UUID = "0d3f5c2e-6a1b-4c8f-9e2d-7b5a1c3d4e6f"
DEVICE_UUID = "9b2c7d1e-4f3a-4b5c-8d6e-1a2b3c4d5e6f"
SAT = "3fa85f64-5717-4562-b3fc-2c963f66afa6"
FAN = "3fa85f64-5717-4562-b3fc-2c963f66afa7"
MODE = "3fa85f64-5717-4562-b3fc-2c963f66afa8"
SCHEDULE = "3fa85f64-5717-4562-b3fc-2c963f66afa9"
WRONG = "3fa85f64-5717-4562-b3fc-2c963f66afaa"
MISSING = "3fa85f64-5717-4562-b3fc-2c963f66afab"
SECRET = "3fa85f64-5717-4562-b3fc-2c963f66afac"
OTHER = "3fa85f64-5717-4562-b3fc-2c963f66afad"
PAGED = "3fa85f64-5717-4562-b3fc-2c963f66afae"
MINUTE = timedelta(minutes=1)

MODEL = f"""\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix ibos: <https://brick.cx2.dk/schema/ibos#> .
@prefix ex: <https://example.com/bldg#> .

ex:Building a brick:Building, ibos:Project ;
    ibos:project-id 4711 ; ibos:project-uuid "{PROJECT_UUID}" .
ex:Forbidden a ibos:Project ; ibos:project-id 4713 .
ex:Elsewhere a ibos:Project ; ibos:project-id 4712 .

ex:SAT a brick:Supply_Air_Temperature_Sensor ;
    ref:hasExternalReference [ ibos:object-uuid "{SAT}" ; ibos:project ex:Building ;
        ibos:object-type "analog-input" ; ibos:object-instance 3 ] .
ex:Fan a brick:Fan_Status ;
    ref:hasExternalReference [ ibos:object-uuid "{FAN}" ; ibos:project ex:Building ] .
ex:Mode a brick:Mode_Status ;
    ref:hasExternalReference [ ibos:object-uuid "{MODE}" ; ibos:project ex:Building ] .
ex:Schedule a brick:Status ;
    ref:hasExternalReference [ ibos:object-uuid "{SCHEDULE}" ;
        ibos:project ex:Building ] .
ex:Wrong a brick:Temperature_Sensor ;
    ref:hasExternalReference [ ibos:object-uuid "{WRONG}" ; ibos:project ex:Building ;
        ibos:object-type "analog-input" ] .
ex:Missing a brick:Temperature_Sensor ;
    ref:hasExternalReference [ ibos:object-uuid "{MISSING}" ;
        ibos:project ex:Building ] .
ex:Secret a brick:Temperature_Sensor ;
    ref:hasExternalReference [ ibos:object-uuid "{SECRET}" ;
        ibos:project ex:Forbidden ] .
ex:Other a brick:Temperature_Sensor ;
    ref:hasExternalReference [ ibos:object-uuid "{OTHER}" ;
        ibos:project ex:Elsewhere ] .
ex:Paged a brick:Temperature_Sensor ;
    ref:hasExternalReference [ ibos:object-uuid "{PAGED}" ;
        ibos:project ex:Building ] .
ex:Malformed a brick:Temperature_Sensor ;
    ref:hasExternalReference [ ibos:object-uuid "nope" ; ibos:project ex:Building ] .
"""


def iso(stamp: datetime) -> str:
    return stamp.astimezone(UTC).isoformat().replace("+00:00", "Z")


class FakeCloud:
    """An in-memory iBOS Data API: three projects, one device, a few objects."""

    def __init__(self) -> None:
        self.now = datetime.now(UTC).replace(microsecond=0)
        self.lock = threading.Lock()
        self.requests = 0
        self.throttle = 0
        self.newest_first = True
        self.objects: dict[str, dict[str, Any]] = {
            SAT: self._object(
                SAT, "analog-input", 3, "AHU01_SAT", "degrees-celsius", 62
            ),
            PAGED: self._object(PAGED, "AnalogInput", 5, "Paged", "°C", None),
            FAN: self._object(FAN, "binaryInput", 1, "Fan", None, 95),
            MODE: self._object(MODE, "multi_state_value", 12, "Mode", None, None),
            SCHEDULE: self._object(SCHEDULE, "schedule", 1, "Sched", None, None),
            WRONG: self._object(WRONG, "binary-value", 7, "Wrong", None, None),
            SECRET: self._object(SECRET, "analog-input", 9, "Secret", None, 62),
        }
        self.project_of = {uuid: 4711 for uuid in self.objects}
        self.project_of[SECRET] = 4713
        self.samples: dict[str, list[dict[str, Any]]] = {
            SAT: [
                self.sample(-90 * MINUTE, 20.5),
                self.sample(-60 * MINUTE, 21.0),
                self.sample(-30 * MINUTE, None),
                self.sample(-1 * MINUTE, 21.5),
            ],
            FAN: [
                self.sample(-50 * MINUTE, 1, "Running"),
                self.sample(-20 * MINUTE, 0, "Stopped"),
            ],
            MODE: [
                self.sample(-70 * MINUTE, 1),
                self.sample(-40 * MINUTE, 2, "Auto"),
                self.sample(-10 * MINUTE, 3, "Manual"),
            ],
            PAGED: [self.sample(-(80 - 10 * i) * MINUTE, float(i)) for i in range(8)],
            SCHEDULE: [],
            WRONG: [self.sample(-5 * MINUTE, 1)],
            SECRET: [self.sample(-5 * MINUTE, 1.0)],
        }

    @staticmethod
    def _object(
        uuid: str,
        object_type: str,
        instance: int,
        name: str,
        units: str | None,
        unit_id: int | None,
    ) -> dict[str, Any]:
        return {
            "id": uuid,
            "object_type": object_type,
            "object_instance": instance,
            "object_name": name,
            "units": units,
            "unit_id": unit_id,
            "device_uuid": DEVICE_UUID,
            "alias": None,
        }

    def sample(
        self, offset: timedelta, value: Any, text: str | None = None
    ) -> dict[str, Any]:
        """A data point; like the API, ``value_text`` repeats the number by default."""
        if text is None and value is not None:
            text = f"{value:g}"
        return {"timestamp": iso(self.now + offset), "value": value, "value_text": text}

    def add_sample(self, uuid: str, value: Any, text: str | None = None) -> None:
        with self.lock:
            self.samples[uuid].append(
                {
                    "timestamp": iso(datetime.now(UTC)),
                    "value": value,
                    "value_text": text,
                }
            )

    def app(self) -> FastAPI:
        app = FastAPI()
        cloud = self

        def gate(authorization: str | None) -> JSONResponse | None:
            with cloud.lock:
                cloud.requests += 1
                if cloud.throttle > 0:
                    cloud.throttle -= 1
                    return JSONResponse(
                        {"error": "Rate limit exceeded."},
                        status_code=429,
                        headers={"Retry-After": "0"},
                    )
            if authorization != f"Bearer {TOKEN}":
                raise HTTPException(401, "Unauthorized")
            return None

        def project_gate(ident: str) -> int:
            number = (
                4711 if ident == PROJECT_UUID else int(ident) if ident.isdigit() else -1
            )
            if number == 4713:
                raise HTTPException(403, "Forbidden")
            if number not in (4711, 4712):
                raise HTTPException(404, "Project not found")
            return number

        @app.get("/api/v1/projects")
        def projects(
            page: int = 1,
            page_size: int = 20,
            authorization: str | None = Header(default=None),
        ) -> Any:
            refused = gate(authorization)
            if refused is not None:
                return refused
            rows = [
                {
                    "id": PROJECT_UUID,
                    "project_id": 4711,
                    "project_name": "School",
                    "device_count": 1,
                    "object_count": 5,
                    "created_at": iso(cloud.now),
                    "updated_at": iso(cloud.now),
                },
                {
                    "id": "11111111-1111-4111-8111-111111111111",
                    "project_id": 4712,
                    "project_name": "Elsewhere",
                    "created_at": iso(cloud.now),
                    "updated_at": iso(cloud.now),
                },
                {
                    "id": "22222222-2222-4222-8222-222222222222",
                    "project_id": 4713,
                    "project_name": "Forbidden",
                    "created_at": iso(cloud.now),
                    "updated_at": iso(cloud.now),
                },
            ]
            start = (page - 1) * page_size
            return {
                "projects": rows[start : start + page_size],
                "total": len(rows),
                "page": page,
                "page_size": page_size,
            }

        @app.get("/api/v1/projects/{ident}")
        def project(
            ident: str, authorization: str | None = Header(default=None)
        ) -> Any:
            refused = gate(authorization)
            if refused is not None:
                return refused
            number = project_gate(ident)
            return {
                "id": PROJECT_UUID if number == 4711 else "x",
                "project_id": number,
                "project_name": "School",
                "created_at": iso(cloud.now),
                "updated_at": iso(cloud.now),
            }

        @app.get("/api/v1/projects/{ident}/devices")
        def project_devices(
            ident: str, authorization: str | None = Header(default=None)
        ) -> Any:
            refused = gate(authorization)
            if refused is not None:
                return refused
            number = project_gate(ident)
            devices = (
                [
                    {
                        "id": DEVICE_UUID,
                        "device_id": 1201,
                        "device_name": "Ctrl",
                        "vendor_name": "Acme",
                        "model_name": "X1",
                        "ip_address": "192.168.10.20",
                    }
                ]
                if number == 4711
                else []
            )
            return {
                "project_uuid": PROJECT_UUID,
                "devices": devices,
                "total": len(devices),
            }

        @app.get("/api/v1/devices/{device_uuid}")
        def device(
            device_uuid: str, authorization: str | None = Header(default=None)
        ) -> Any:
            refused = gate(authorization)
            if refused is not None:
                return refused
            if device_uuid != DEVICE_UUID:
                raise HTTPException(404, "Device not found")
            return {"id": DEVICE_UUID, "device_id": 1201, "device_name": "Ctrl"}

        @app.get("/api/v1/devices/{device_uuid}/objects")
        def device_objects(
            device_uuid: str, authorization: str | None = Header(default=None)
        ) -> Any:
            refused = gate(authorization)
            if refused is not None:
                return refused
            if device_uuid != DEVICE_UUID:
                raise HTTPException(404, "Device not found")
            rows = [
                {
                    key: item[key]
                    for key in (
                        "id",
                        "object_type",
                        "object_instance",
                        "object_name",
                        "units",
                    )
                }
                for uuid, item in cloud.objects.items()
                if cloud.project_of[uuid] == 4711
            ]
            return {
                "device_id": DEVICE_UUID,
                "device_name": "Ctrl",
                "objects": rows,
                "total": len(rows),
            }

        def object_gate(object_uuid: str) -> dict[str, Any]:
            item = cloud.objects.get(object_uuid)
            if item is None:
                raise HTTPException(404, "Object not found")
            if cloud.project_of[object_uuid] == 4713:
                raise HTTPException(403, "Forbidden")
            return item

        @app.get("/api/v1/objects/{object_uuid}")
        def one_object(
            object_uuid: str, authorization: str | None = Header(default=None)
        ) -> Any:
            refused = gate(authorization)
            if refused is not None:
                return refused
            return object_gate(object_uuid)

        @app.get("/api/v1/objects/{object_uuid}/trending")
        def trending(
            object_uuid: str,
            start_time: str | None = None,
            end_time: str | None = None,
            limit: int = Query(default=1000),
            authorization: str | None = Header(default=None),
        ) -> Any:
            refused = gate(authorization)
            if refused is not None:
                return refused
            item = object_gate(object_uuid)
            start = (
                datetime.fromisoformat(start_time.replace("Z", "+00:00"))
                if start_time
                else cloud.now - timedelta(days=7)
            )
            end = (
                datetime.fromisoformat(end_time.replace("Z", "+00:00"))
                if end_time
                else datetime.now(UTC)
            )
            with cloud.lock:
                rows = [
                    row
                    for row in cloud.samples[object_uuid]
                    if start
                    <= datetime.fromisoformat(row["timestamp"].replace("Z", "+00:00"))
                    <= end
                ]
            rows.sort(key=lambda row: str(row["timestamp"]), reverse=cloud.newest_first)
            page = rows[:limit]
            return {
                "object_uuid": object_uuid,
                "object_name": item["object_name"],
                "data_points": page,
                "total": len(page),
                "sampled": False,
                "start_time": iso(start),
                "end_time": iso(end),
            }

        return app


@pytest.fixture
def cloud() -> Iterator[tuple[FakeCloud, str]]:
    """A fake iBOS Data API served by uvicorn in a thread on a free port."""
    fake = FakeCloud()
    port = free_port()
    server = uvicorn.Server(
        uvicorn.Config(fake.app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, name="fake-ibos", daemon=True)
    thread.start()
    wait_for(lambda: server.started)
    yield fake, f"http://127.0.0.1:{port}"
    server.should_exit = True
    thread.join(5)


@functools.cache
def model_graph() -> GraphReader:
    """The model as the daemon holds it: validated, inferred and joined with the
    iBOS vocabulary, built once for the module."""
    return graph_from_turtle(MODEL)


def assigned(name: str, interval: float = 0.4) -> AssignedPoint:
    return assigned_point(f"{EX}{name}", interval=interval)


def config_for(url: str, **overrides: Any) -> IBOSConfig:
    data: dict[str, Any] = {
        "token": TOKEN,
        "url": url,
        "projects": [4711, 4713],
        "backfill": "2h",
        "overlap": "15m",
        "rate_limit": 500,
        "timeout": "5s",
    }
    data.update(overrides)
    return IBOSConfig.model_validate(data)


def test_value_helpers() -> None:
    assert normalise_type("analogInput") == "analog-input"
    assert normalise_type("MULTI_STATE_VALUE") == "multi-state-value"
    assert normalise_type(" binary value ") == "binary-value"
    assert normalise_type("MultiStateInput") == "multi-state-input"
    assert normalise_type("BinaryInput") == "binary-input"
    assert normalise_type(None) is None
    assert value_type_for("analog-input") == "number"
    assert value_type_for("schedule") is None
    assert convert("number", 21.5, None).value == 21.5
    assert convert("number", None, "21.5").value == 21.5
    assert convert("number", None, None).reason == "no_value"
    assert convert("integer", 3.0, None).value == 3
    boolean = convert("boolean", 1, "Running")
    assert (boolean.type, boolean.value, boolean.text) == (
        "boolean",
        True,
        (1, "Running"),
    )
    assert convert("boolean", None, "inactive").value is False
    mode = convert("enum", 2, "Auto")
    assert (mode.value, mode.text) == (2, (2, "Auto"))
    assert convert("enum", 3.0, "3").text is None, "the number itself teaches nothing"
    assert convert("boolean", 0.0, "0").text is None
    assert convert("boolean", 0.0, "0").value is False
    assert convert("string", None, "hello").value == "hello"
    stamp = convert("datetime", None, "2026-09-07T10:00:00Z").value
    assert stamp == datetime(2026, 9, 7, 10, tzinfo=UTC)
    assert unit_for(62, None) == QUDT + "DEG_C"
    assert unit_for(95, "no-units") is None
    assert unit_for(None, "degrees-celsius") == QUDT + "DEG_C"
    assert unit_for(None, "°C") == QUDT + "DEG_C"
    assert unit_for(None, "m³/h") == QUDT + "M3-PER-HR"
    assert unit_for(None, "%RH") == QUDT + "PERCENT_RH"
    assert unit_for(None, "%") == QUDT + "PERCENT"
    assert unit_for(None, "furlongs") == "furlongs"
    assert unit_for(None, "") is None
    assert unit_for(None, None) is None


def test_configuration_is_checked() -> None:
    config = IBOSConfig.model_validate(
        {"token": "t", "projects": ["4711", PROJECT_UUID.upper()], "backfill": 0}
    )
    assert config.projects == [4711, PROJECT_UUID]
    assert config.backfill == timedelta(0)
    assert config.claims_project(4711, None) and config.claims_project(1, PROJECT_UUID)
    assert not config.claims_project(4712, None)
    assert IBOSConfig(token="t").claims_project(4712, None), "unrestricted"
    with pytest.raises(ValidationError, match="number or its UUID"):
        IBOSConfig.model_validate({"token": "t", "projects": ["school"]})
    with pytest.raises(ValidationError, match="http"):
        IBOSConfig.model_validate({"token": "t", "url": "api.example.com"})
    with pytest.raises(ValidationError):
        IBOSConfig.model_validate({"token": "t", "rate_limit": 0})
    assert (
        IBOSConfig.model_validate({"token": "t", "url": "https://x.example/"}).url
        == "https://x.example"
    )


def test_source_fetches_history_from_the_cloud(cloud: tuple[FakeCloud, str]) -> None:
    fake, url = cloud
    source = IBOSSource("ibos_test", config_for(url), model_graph())
    assert list(source.resources()) == []
    claimed = source.claim()
    assert {f"{EX}SAT", f"{EX}Fan", f"{EX}Missing", f"{EX}Secret"} <= claimed
    assert f"{EX}Malformed" in claimed, (
        "in scope by its project, so claimed and rejected"
    )
    assert f"{EX}Other" not in claimed, "project 4712 is out of scope"

    fake.throttle = 2
    collector = Collector()
    thread = threading.Thread(
        target=source.start, args=(collector, collector), daemon=True
    )
    thread.start()
    try:
        source.assign(
            [
                assigned("SAT"),
                assigned("Fan"),
                assigned("Mode"),
                assigned("Schedule"),
                assigned("Wrong"),
                assigned("Missing"),
                assigned("Secret"),
                assigned("Malformed"),
            ]
        )
        wait_for(lambda: len(collector.observations_for(f"{EX}SAT")) >= 4)
        wait_for(lambda: len(collector.observations_for(f"{EX}Mode")) >= 3)
        wait_for(lambda: len(collector.observations_for(f"{EX}Fan")) >= 2)
        wait_for(
            lambda: (
                (collector.outcome_of(f"{EX}Missing") or Outcome("", "active")).state
                == "rejected"
            )
        )

        sat = collector.observations_for(f"{EX}SAT")
        assert [o.timestamp for o in sat] == [
            fake.now - 90 * MINUTE,
            fake.now - 60 * MINUTE,
            fake.now - 30 * MINUTE,
            fake.now - 1 * MINUTE,
        ], "every sample keeps the cloud's timestamp"
        assert [(o.type, o.value, o.reason) for o in sat] == [
            ("number", 20.5, None),
            ("number", 21.0, None),
            ("null", None, "no_value"),
            ("number", 21.5, None),
        ]
        time.sleep(1.2)
        assert len(collector.observations_for(f"{EX}SAT")) == 4, (
            "re-read, never re-delivered"
        )
        fake.add_sample(SAT, 22.0)
        wait_for(lambda: len(collector.observations_for(f"{EX}SAT")) == 5)
        assert collector.observations_for(f"{EX}SAT")[-1].value == 22.0

        assert [(o.type, o.value) for o in collector.observations_for(f"{EX}Fan")] == [
            ("boolean", True),
            ("boolean", False),
        ]
        assert [(o.type, o.value) for o in collector.observations_for(f"{EX}Mode")] == [
            ("enum", 1),
            ("enum", 2),
            ("enum", 3),
        ]
        wait_for(
            lambda: (
                (collector.metadata_for(f"{EX}Mode") or PointMetadata("")).enum_texts
                == {2: "Auto", 3: "Manual"}
            )
        )
        assert 1 not in (
            (collector.metadata_for(f"{EX}Mode") or PointMetadata("")).enum_texts or {}
        ), "a value_text that repeats the number is not a state text"
        sat_metadata = collector.metadata_for(f"{EX}SAT")
        assert sat_metadata is not None
        assert (sat_metadata.value_type, sat_metadata.unit) == (
            "number",
            QUDT + "DEG_C",
        )
        fan_metadata = collector.metadata_for(f"{EX}Fan")
        assert fan_metadata is not None
        assert (fan_metadata.value_type, fan_metadata.unit) == ("boolean", None)
        assert fan_metadata.boolean_texts == ("Stopped", "Running")

        def reason(name: str) -> str:
            outcome = collector.outcome_of(f"{EX}{name}")
            assert outcome is not None and outcome.state == "rejected", name
            return outcome.reason or ""

        assert (
            collector.outcome_of(f"{EX}SAT") or Outcome("", "rejected")
        ).state == "active"
        assert "no counterpart" in reason("Schedule")
        assert "the graph says object type 'analog-input'" in reason("Wrong")
        assert "not in iBOS" in reason("Missing")
        assert "malformed object uuid" in reason("Malformed")
        assert (
            collector.outcome_of(f"{EX}Secret") or Outcome("", "rejected")
        ).state == "active"
        assert collector.observations_for(f"{EX}Secret") == []

        def devices() -> list[dict[str, Any]]:
            with collector.lock:
                return list(collector.devices)

        wait_for(
            lambda: any(
                d["device"] == "project-4713" and not d["reachable"] for d in devices()
            )
        )
        forbidden = next(d for d in devices() if d["device"] == "project-4713")
        assert "denied" in (forbidden["error"] or "")
        assert any(d["device"] == "project-4711" and d["reachable"] for d in devices())

        assert source.stats.throttled == 2, "the 429s were waited out"
        assert source.stats.requests > 2
        with collector.lock:
            assert "rate_limited" not in collector.warnings
    finally:
        source.stop()
        thread.join(10)
    assert not thread.is_alive(), "the source's loop ends when stopped"


def test_a_point_resumes_from_the_assignments_latest_observation(
    cloud: tuple[FakeCloud, str],
) -> None:
    """Paged has eight samples, ten minutes apart from 80 to 10 minutes ago;
    with the latest observation 35 minutes ago and an overlap of 15, the
    fetch starts 50 minutes ago and yields five, not the backfill's eight."""
    fake, url = cloud
    source = IBOSSource("ibos_resume", config_for(url), model_graph())
    collector = Collector()
    thread = threading.Thread(
        target=source.start, args=(collector, collector), daemon=True
    )
    thread.start()
    try:
        point = assigned("Paged", 0.3)
        source.assign(
            [
                AssignedPoint(
                    point.uri,
                    point.method,
                    point.parameters,
                    last_observation=fake.now - 35 * MINUTE,
                )
            ]
        )
        wait_for(lambda: len(collector.observations_for(f"{EX}Paged")) >= 5)
        time.sleep(0.8)
        stamps = sorted(o.timestamp for o in collector.observations_for(f"{EX}Paged"))
        assert len(stamps) == 5, stamps
        assert stamps[0] == fake.now - 50 * MINUTE, "from the latest minus overlap"
    finally:
        source.stop()
        thread.join(10)


def test_the_budget_stretches_rounds_and_warns(cloud: tuple[FakeCloud, str]) -> None:
    _, url = cloud
    source = IBOSSource("ibos_slow", config_for(url, rate_limit=4), model_graph())
    collector = Collector()
    thread = threading.Thread(
        target=source.start, args=(collector, collector), daemon=True
    )
    started = time.monotonic()
    thread.start()
    try:
        source.assign(
            [assigned("SAT", 0.3), assigned("Fan", 0.3), assigned("Mode", 0.3)]
        )
        wait_for(lambda: "rate_limited" in collector.warnings, 20.0)
        with collector.lock:
            message = collector.warnings["rate_limited"]
        assert "4 requests per second" in message and "the interval is 0s" in message
        elapsed = time.monotonic() - started
        assert source.stats.requests <= 4 * elapsed + 2, (
            "requests stay under the budget"
        )
        assert len(collector.observations_for(f"{EX}SAT")) == 4
    finally:
        source.stop()
        thread.join(10)


def test_an_invalid_token_fails_the_instance(cloud: tuple[FakeCloud, str]) -> None:
    _, url = cloud
    source = IBOSSource("ibos_bad", config_for(url, token="wrong"), model_graph())
    collector = Collector()
    source.assign([assigned("SAT", 0.2)])
    with pytest.raises(Unauthorized, match="401"):
        source.start(collector, collector)
    assert collector.observations_for(f"{EX}SAT") == []


def test_tools_run_without_a_started_source(cloud: tuple[FakeCloud, str]) -> None:
    _, url = cloud
    source = IBOSSource("ibos_tools", config_for(url), model_graph())
    projects = source.run_tool("projects", {})
    assert {row["number"] for row in projects} == {4711, 4712, 4713}
    assert (
        next(row for row in projects if row["number"] == 4711)["uuid"] == PROJECT_UUID
    )

    devices = source.run_tool("devices", {"project": "4711"})
    assert devices == [
        {
            "uuid": DEVICE_UUID,
            "number": 1201,
            "name": "Ctrl",
            "vendor": "Acme",
            "model": "X1",
            "ip": "192.168.10.20",
        }
    ]

    objects = {
        row["uuid"]: row
        for row in source.run_tool("objects", {"device": DEVICE_UUID, "values": True})
    }
    assert (
        objects[SAT]["type"] == "analog-input"
        and objects[SAT]["qudt"] == QUDT + "DEG_C"
    )
    assert (objects[SAT]["type"], objects[SAT]["value"]) == ("analog-input", 21.5)
    assert objects[FAN]["type"] == "binary-input" and objects[FAN]["value"] is False
    assert objects[SCHEDULE]["value"] is None

    rows = source.run_tool("read", {"object": SAT, "since": "3h", "limit": 2})
    assert [(row["type"], row["value"]) for row in rows] == [
        ("number", 21.5),
        ("null", None),
    ]
    assert rows[1]["reason"] == "no_value" and rows[0]["raw_value"] == 21.5

    with pytest.raises(ToolError, match="not found"):
        source.run_tool("read", {"object": MISSING})
    with pytest.raises(ToolError, match="running daemon"):
        source.run_tool("resolve", {"point": "ex:SAT"})
    with pytest.raises(ToolError, match="access denied"):
        source.run_tool("devices", {"project": "4713"})


def test_pointlist_nests_projects_devices_and_objects(
    cloud: tuple[FakeCloud, str],
) -> None:
    _, url = cloud
    source = IBOSSource(
        "ibos_list", config_for(url, projects=[4711, 4712]), model_graph()
    )
    document = source.run_tool("pointlist", {})
    assert document["instance"] == "ibos_list" and document["url"] == url
    assert document["exported_at"].endswith("+00:00")
    assert document["counts"] == {"projects": 2, "devices": 1, "objects": 6}
    assert [project["number"] for project in document["projects"]] == [4711, 4712], (
        "the claimed projects, not the forbidden third"
    )
    school = document["projects"][0]
    assert set(school) == {"number", "uuid", "name", "description", "devices"}
    assert school["uuid"] == PROJECT_UUID and school["name"] == "School"
    device = school["devices"][0]
    assert device["uuid"] == DEVICE_UUID and device["ip"] == "192.168.10.20"
    objects = {row["uuid"]: row for row in device["objects"]}
    assert objects[SAT]["type"] == "analog-input"
    assert objects[SAT]["qudt"] == QUDT + "DEG_C"
    assert set(objects[SAT]) == {"uuid", "type", "instance", "name", "unit", "qudt"}
    assert document["projects"][1]["devices"] == []

    one = source.run_tool("pointlist", {"project": PROJECT_UUID})
    assert one["counts"]["projects"] == 1 and one["projects"][0]["number"] == 4711
    assert one["counts"]["objects"] == 6

    with pytest.raises(ToolError, match="access denied"):
        source.run_tool("pointlist", {"project": "4713"})
    with pytest.raises(ToolError, match="not found"):
        source.run_tool("pointlist", {"project": "4799"})
    claims_forbidden = IBOSSource("ibos_all", config_for(url), model_graph())
    with pytest.raises(ToolError, match="access denied"):
        claims_forbidden.run_tool("pointlist", {})  # 4713 is claimed but refused


def test_resolve_runs_inside_a_started_source(cloud: tuple[FakeCloud, str]) -> None:
    _, url = cloud
    source = IBOSSource("ibos_live", config_for(url), model_graph())
    collector = Collector()
    thread = threading.Thread(
        target=source.start, args=(collector, collector), daemon=True
    )
    thread.start()
    try:
        wait_for(lambda: source.client is not None)
        result = source.run_tool("resolve", {"point": "ex:SAT"})
        assert result["object"] == SAT and result["project"] == 4711
        assert result["in_scope"] is True and result["check"] == "ok"
        assert (result["type"], result["value_type"], result["value"]) == (
            "analog-input",
            "number",
            21.5,
        )
        wrong = source.run_tool("resolve", {"point": "ex:Wrong"})
        assert "the graph says object type 'analog-input'" in wrong["check"]
        other = source.run_tool("resolve", {"point": "ex:Other"})
        assert other["in_scope"] is False
        missing = source.run_tool("resolve", {"point": "ex:Missing"})
        assert "not found" in missing["error"]
        broken = source.run_tool("resolve", {"point": "ex:Malformed"})
        assert "malformed object uuid" in broken["problem"]
    finally:
        source.stop()
        thread.join(10)


def test_declaration_has_a_factory_and_tools() -> None:
    assert SOURCE.factory is IBOSSource
    assert [tool.name for tool in SOURCE.tools] == [
        "projects",
        "devices",
        "objects",
        "read",
        "resolve",
        "pointlist",
    ]
    assert [tool.document for tool in SOURCE.tools] == [False] * 5 + [True]
    assert SOURCE.tools[-1].offered_on == ToolOffer("projects", {"project": "number"})
    with pytest.raises(ValueError, match="only a document"):
        ToolDeclaration("x", "", ProjectsParameters, offered_on=ToolOffer("y", {}))
    with pytest.raises(ValueError, match="its own rows"):
        ToolDeclaration(
            "x", "", ProjectsParameters, document=True, offered_on=ToolOffer("x", {})
        )
    with pytest.raises(ValueError, match="has no tool of"):
        SourceDeclaration(
            type_name="t",
            description="",
            config_schema=IBOSConfig,
            reference_types=(),
            tools=(
                ToolDeclaration(
                    "x",
                    "",
                    ProjectsParameters,
                    document=True,
                    offered_on=ToolOffer("missing", {}),
                ),
            ),
        )


@pytest.mark.parametrize("newest_first", [True, False])
def test_pages_follow_the_order_the_api_serves(
    cloud: tuple[FakeCloud, str], newest_first: bool
) -> None:
    fake, url = cloud
    fake.newest_first = newest_first
    source = IBOSSource("ibos_pages", config_for(url), model_graph())
    source.page_size = 3
    collector = Collector()
    thread = threading.Thread(
        target=source.start, args=(collector, collector), daemon=True
    )
    thread.start()
    try:
        source.assign([assigned("Paged", 0.3)])
        wait_for(lambda: len(collector.observations_for(f"{EX}Paged")) >= 8)
        time.sleep(0.8)
        paged = collector.observations_for(f"{EX}Paged")
        assert [o.value for o in paged] == [float(i) for i in range(8)], (
            "every page, each sample once"
        )
        assert [o.timestamp for o in paged] == [
            fake.now - (80 - 10 * i) * MINUTE for i in range(8)
        ]
        metadata = collector.metadata_for(f"{EX}Paged")
        assert metadata is not None and metadata.unit == QUDT + "DEG_C", (
            "the unit symbol alone is translated"
        )
    finally:
        source.stop()
        thread.join(10)


def test_every_round_that_succeeds_reports_the_project(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The project's last success follows the rounds. It was reported only
    when reachability changed, so on a machine that never lost the cloud
    `sources status` showed the first success for weeks."""
    reports: list[tuple[str, bool]] = []

    class Status:
        def device(
            self,
            device: str,
            *,
            reachable: bool,
            error: str | None = None,
            skipped_rounds: int | None = None,
        ) -> None:
            reports.append((device, reachable))

    interval = timedelta(minutes=5)
    source = cast(IBOSSource, SimpleNamespace(status=Status()))
    fetcher = ProjectFetcher(source, 4711)
    point = SimpleNamespace(interval=interval, rejected=False, metadata_sent=True)
    fetcher.points = {"p": cast(FetchPoint, point)}

    async def fetched(fp: FetchPoint) -> None:
        return None

    monkeypatch.setattr(fetcher, "_fetch_samples", fetched)
    asyncio.run(fetcher._round(interval))
    asyncio.run(fetcher._round(interval))
    assert reports == [("project-4711", True)] * 3, "the change, then every round"
