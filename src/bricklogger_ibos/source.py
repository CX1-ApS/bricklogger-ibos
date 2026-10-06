"""The iBOS source: a history source that fetches, per object, the samples the
cloud received since the round before, under a request budget, and passes them
on with the cloud's timestamps. See ``README.md``.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import threading
import time
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from bricklogger.sdk.bacnet import UNSUPPORTED
from bricklogger.sdk.contract import (
    AssignedPoint,
    GraphReader,
    Observation,
    Outcome,
    PointMetadata,
    Sink,
    Source,
    StatusChannel,
    ValueType,
)
from pydantic import BaseModel

from bricklogger_ibos import tools
from bricklogger_ibos.client import (
    TRENDING_LIMIT,
    Forbidden,
    IBOSClient,
    NotFound,
    Stats,
    Unauthorized,
    Unavailable,
)
from bricklogger_ibos.config import IBOSConfig
from bricklogger_ibos.references import (
    IBOSReference,
    ResolvedReferences,
    resolve_references,
)
from bricklogger_ibos.values import (
    Sample,
    convert,
    newest_first,
    normalise_type,
    samples_of,
    unit_for,
    value_type_for,
)

log = logging.getLogger(__name__)

DEFAULT_INTERVAL = timedelta(minutes=5)
MAX_IN_FLIGHT = 8
"""Requests in the air at once; the budget paces their starts."""
MAX_PAGES = 10_000
RATE_LIMITED = "rate_limited"


@dataclass
class FetchPoint:
    """One assigned point: its reference, its rhythm and how far it has been read."""

    reference: IBOSReference
    interval: timedelta
    value_type: ValueType | None = None
    unit: str | None = None
    metadata_sent: bool = False
    rejected: bool = False
    last_timestamp: datetime | None = None
    delivered: set[datetime] = field(default_factory=set)
    enum_texts: dict[int, str] = field(default_factory=dict)
    boolean_texts: dict[int, str] = field(default_factory=dict)


class RoundState:
    """What stops a round early: the first answer that makes the rest pointless."""

    def __init__(self) -> None:
        self.failed: Exception | None = None
        self.succeeded = False


class IBOSSource(Source):
    """One ``ibos`` instance: its own client, budget and loop; a fetcher per project."""

    page_size: int = TRENDING_LIMIT
    """Samples per trending request: the API's maximum, lowered only in tests."""

    def __init__(self, name: str, config: BaseModel, graph: GraphReader) -> None:
        super().__init__(name, config, graph)
        self.settings = (
            config
            if isinstance(config, IBOSConfig)
            else IBOSConfig.model_validate(config.model_dump())
        )
        self._lock = threading.Lock()
        self._assignment: list[AssignedPoint] = []
        self._resolved: ResolvedReferences | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event: asyncio.Event | None = None
        self._fatal: BaseException | None = None
        #: Set while ``start`` runs, and ``_ready`` once the loop and the client
        #: exist, so a tool asked for during start-up waits for them instead of
        #: opening a client of its own.
        self._starting = threading.Event()
        self._ready = threading.Event()
        # assumed until a round keeps up, so a warning left from an earlier run ends
        self._rate_limited = True
        self.sink: Sink | None = None
        self.status: StatusChannel | None = None
        self.client: IBOSClient | None = None
        self.stats = Stats()
        self.projects: dict[int, ProjectFetcher] = {}

    # --- binding -------------------------------------------------------------

    def resources(self) -> Iterable[str]:
        return []

    def claim(self) -> set[str]:
        """Every point with an iBOS reference whose project is in this instance's scope.

        A reference the source recognises but cannot use is claimed too, when
        its project is in scope or the instance has no restriction, so that the
        point is reported as rejected rather than unclaimed.
        """
        resolved = resolve_references(self.graph)
        with self._lock:
            self._resolved = resolved
        settings = self.settings
        claimed = {
            point
            for point, reference in resolved.references.items()
            if settings.claims_project(reference.project_id, reference.project_uuid)
        }
        for point, problem in resolved.problems.items():
            if problem.project_id is not None:
                if settings.claims_project(problem.project_id, problem.project_uuid):
                    claimed.add(point)
            elif not settings.restricted:
                claimed.add(point)
        return claimed

    # --- operation -----------------------------------------------------------

    def start(self, sink: Sink, status: StatusChannel) -> None:
        self.sink, self.status = sink, status
        self._starting.set()
        try:
            asyncio.run(self._run())
        finally:
            self._starting.clear()

    def assign(self, points: Sequence[AssignedPoint]) -> None:
        with self._lock:
            self._assignment = list(points)
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._reconcile)

    def stop(self) -> None:
        loop, event = self._loop, self._stop_event
        if loop is not None and event is not None and loop.is_running():
            loop.call_soon_threadsafe(event.set)

    def fail(self, exc: BaseException) -> None:
        """End the loop with an error: the daemon marks the instance failed and
        restarts it with backoff."""
        self._fatal = exc
        if self._stop_event is not None:
            self._stop_event.set()

    async def _run(self) -> None:
        self._loop = asyncio.get_running_loop()
        self._stop_event = asyncio.Event()
        self._fatal = None
        self.client = IBOSClient(
            self.settings, stats=self.stats, page_size=self.page_size
        )
        self._ready.set()
        try:
            self._reconcile()
            await self._stop_event.wait()
            if self._fatal is not None:
                raise self._fatal
        finally:
            self._ready.clear()
            for fetcher in self.projects.values():
                fetcher.cancel()
            self.projects.clear()
            await asyncio.sleep(0)
            await self.client.close()
            self.client = None
            self._loop = None
            self._stop_event = None

    def _reconcile(self) -> None:
        """Apply the assignment: resolve, group by project, start and stop fetchers."""
        with self._lock:
            assignment = list(self._assignment)
            resolved = self._resolved
        if resolved is None or any(
            point.uri not in resolved.references and point.uri not in resolved.problems
            for point in assignment
        ):
            resolved = resolve_references(self.graph)
            with self._lock:
                self._resolved = resolved
        outcomes: list[Outcome] = []
        wanted: dict[int, dict[str, FetchPoint]] = {}
        for point in assignment:
            reference = resolved.references.get(point.uri)
            if reference is None:
                problem = resolved.problems.get(point.uri)
                reason = problem.reason if problem else "no iBOS reference found"
                outcomes.append(Outcome(point.uri, "rejected", reason))
                continue
            if point.method != "poll":
                outcomes.append(
                    Outcome(
                        point.uri,
                        "unsupported",
                        f"method {point.method!r} is not offered",
                    )
                )
                continue
            interval = point.parameters.get("interval") or DEFAULT_INTERVAL
            wanted.setdefault(reference.project_id, {})[point.uri] = FetchPoint(
                reference, interval, last_timestamp=point.last_observation
            )
            outcomes.append(Outcome(point.uri, "active"))
        for project_id in set(self.projects) - set(wanted):
            self.projects.pop(project_id).cancel()
        for project_id, points in wanted.items():
            fetcher = self.projects.get(project_id)
            if fetcher is None:
                fetcher = ProjectFetcher(self, project_id)
                self.projects[project_id] = fetcher
            fetcher.set_points(points)
        if self.status is not None and outcomes:
            self.status.outcomes(outcomes)

    def review_capacity(self) -> None:
        """Warn while a round outlasts its interval; withdraw it when all fit."""
        status = self.status
        if status is None:
            return
        over: list[tuple[float, float, int]] = []
        for fetcher in self.projects.values():
            for interval, (duration, count) in fetcher.last_rounds.items():
                seconds = interval.total_seconds()
                if duration > seconds:
                    over.append((duration, seconds, count))
        if over:
            duration, seconds, count = max(over, key=lambda item: item[0] - item[1])
            status.warn(
                RATE_LIMITED,
                f"a round of {count} points takes {duration:.0f}s at "
                f"{self.settings.rate_limit:g} requests per second; "
                f"the interval is {seconds:.0f}s",
            )
            self._rate_limited = True
        elif self._rate_limited:
            status.clear_warning(RATE_LIMITED)
            self._rate_limited = False

    # --- tools ---------------------------------------------------------------

    def run_tool(self, name: str, parameters: Mapping[str, Any]) -> Any:
        """Run a protocol tool: on the source's own loop and client while it runs,
        and on a client of its own when the source is not started.
        """
        if self._starting.is_set() and not self._ready.is_set():
            # the daemon reports the instance running before the loop is up
            self._ready.wait(5.0)
        loop, client = self._loop, self.client
        if loop is not None and loop.is_running() and client is not None:
            future = asyncio.run_coroutine_threadsafe(
                tools.run(
                    name, client, self.settings, self.graph, parameters, self.name
                ),
                loop,
            )
            return future.result(timeout=tools.timeout_for(name, parameters))
        if name == "resolve":
            raise tools.ToolError("resolve needs the running daemon's working graph")
        return asyncio.run(self._run_offline(name, parameters))

    async def _run_offline(self, name: str, parameters: Mapping[str, Any]) -> Any:
        client = IBOSClient(self.settings)
        try:
            return await tools.run(
                name, client, self.settings, None, parameters, self.name
            )
        finally:
            await client.close()

    def reject(self, point: str, reason: str) -> None:
        """Report a point as rejected after the API showed it cannot be logged."""
        if self.status is not None:
            self.status.outcomes([Outcome(point, "rejected", reason)])


class ProjectFetcher:
    """All the points of one project: a round per interval, metadata and status.

    The project is the status device: reachable while its requests succeed.
    """

    def __init__(self, source: IBOSSource, project_id: int) -> None:
        self.source = source
        self.project_id = project_id
        self.device = f"project-{project_id}"
        self.points: dict[str, FetchPoint] = {}
        self.tasks: dict[timedelta, asyncio.Task[None]] = {}
        self.last_rounds: dict[timedelta, tuple[float, int]] = {}
        self.reachable: bool | None = None

    def set_points(self, points: dict[str, FetchPoint]) -> None:
        """Take a new desired state, keeping the progress of points that stay."""
        merged: dict[str, FetchPoint] = {}
        for uri, wanted in points.items():
            existing = self.points.get(uri)
            if existing is not None and existing.reference == wanted.reference:
                existing.interval = wanted.interval
                if existing.last_timestamp is None:
                    existing.last_timestamp = wanted.last_timestamp
                merged[uri] = existing
            else:
                merged[uri] = wanted
        self.points = {uri: fp for uri, fp in merged.items() if not fp.rejected}
        intervals = {fp.interval for fp in self.points.values()}
        for interval in set(self.tasks) - intervals:
            self.tasks.pop(interval).cancel()
            self.last_rounds.pop(interval, None)
        for interval in intervals - set(self.tasks):
            self.tasks[interval] = asyncio.create_task(self._round_loop(interval))

    def cancel(self) -> None:
        for task in self.tasks.values():
            task.cancel()
        self.tasks.clear()

    async def _round_loop(self, interval: timedelta) -> None:
        seconds = interval.total_seconds()
        spread = int(
            hashlib.sha1(f"{self.project_id}:{seconds}".encode()).hexdigest()[:4], 16
        )
        await asyncio.sleep(min(seconds, 2.0) * spread / 0xFFFF)
        while True:
            started = time.monotonic()
            count = 0
            try:
                count = await self._round(interval)
            except asyncio.CancelledError:
                raise
            except Unauthorized as exc:
                self.source.fail(exc)
                return
            except Exception:
                log.exception(
                    "%s: a round for project %d failed",
                    self.source.name,
                    self.project_id,
                )
            elapsed = time.monotonic() - started
            self.last_rounds[interval] = (elapsed, count)
            self.source.review_capacity()
            await asyncio.sleep(max(0.0, seconds - elapsed))

    async def _round(self, interval: timedelta) -> int:
        """Fetch every point of the interval once; the budget paces the requests."""
        points = [fp for fp in self.points.values() if fp.interval == interval]
        if not points:
            return 0
        state = RoundState()
        semaphore = asyncio.Semaphore(MAX_IN_FLIGHT)

        async def one(fp: FetchPoint) -> None:
            async with semaphore:
                await self._fetch_point(fp, state)

        await asyncio.gather(*(one(fp) for fp in points))
        if isinstance(state.failed, Unauthorized):
            raise state.failed
        if state.failed is None and state.succeeded:
            # once per round, so the device's last success follows the rounds
            self._report(reachable=True, round_done=True)
        return len(points)

    async def _fetch_point(self, fp: FetchPoint, state: RoundState) -> None:
        if state.failed is not None or fp.rejected:
            return
        try:
            if not fp.metadata_sent:
                await self._read_metadata(fp)
                if fp.rejected:
                    return
            await self._fetch_samples(fp)
            state.succeeded = True
            self._report(reachable=True)
        except NotFound:
            self._reject(fp, "the object is not in iBOS")
        except Unauthorized as exc:
            state.failed = exc
        except Forbidden as exc:
            state.failed = exc
            self._report(
                reachable=False,
                error=f"access to the project is denied ({exc.status})",
            )
        except Unavailable as exc:
            state.failed = exc
            self._report(reachable=False, error=exc.message)

    # --- metadata ------------------------------------------------------------

    async def _read_metadata(self, fp: FetchPoint) -> None:
        """The object as iBOS knows it: the value type, the unit, and the check
        of the graph's type and instance number against the API's."""
        client = self.source.client
        if client is None:
            return
        data = await client.object(fp.reference.object_uuid)
        reference = fp.reference
        api_type = normalise_type(data.get("object_type"))
        api_instance = data.get("object_instance")
        if (
            reference.object_type is not None
            and api_type is not None
            and reference.object_type != api_type
        ):
            self._reject(
                fp,
                f"the graph says object type {reference.object_type!r}; "
                f"iBOS says {api_type!r}",
            )
            return
        if (
            reference.object_instance is not None
            and isinstance(api_instance, int)
            and not isinstance(api_instance, bool)
            and reference.object_instance != api_instance
        ):
            self._reject(
                fp,
                f"the graph says object instance {reference.object_instance}; "
                f"iBOS says {api_instance}",
            )
            return
        value_type = value_type_for(api_type)
        if value_type is None:
            self._reject(
                fp, UNSUPPORTED if api_type else "iBOS gives the object no type"
            )
            return
        fp.value_type = value_type
        fp.unit = unit_for(data.get("unit_id"), data.get("units"))
        self._send_metadata(fp)

    def _send_metadata(self, fp: FetchPoint) -> None:
        sink = self.source.sink
        fp.metadata_sent = True
        if sink is None or fp.value_type is None:
            return
        enum_texts = (
            dict(fp.enum_texts) if fp.value_type == "enum" and fp.enum_texts else None
        )
        boolean_texts: tuple[str, str] | None = None
        if fp.value_type == "boolean" and fp.boolean_texts:
            boolean_texts = (
                fp.boolean_texts.get(0, "inactive"),
                fp.boolean_texts.get(1, "active"),
            )
        sink.metadata(
            [
                PointMetadata(
                    fp.reference.point,
                    fp.value_type,
                    fp.unit,
                    enum_texts,
                    boolean_texts,
                )
            ]
        )

    # --- samples -------------------------------------------------------------

    async def _fetch_samples(self, fp: FetchPoint) -> None:
        """Everything since the latest known sample minus the overlap, in pages
        that follow the order the API serves: the newest first, so each page
        reaches further back than the one before."""
        client = self.source.client
        if client is None or fp.value_type is None:
            return
        settings = self.source.settings
        now = datetime.now(UTC)
        if fp.last_timestamp is None:
            start = now - settings.backfill
        else:
            start = fp.last_timestamp - settings.overlap
        fp.delivered = {stamp for stamp in fp.delivered if stamp >= start}
        page_start, page_end = start, now
        for _ in range(MAX_PAGES):
            data = await client.trending(fp.reference.object_uuid, page_start, page_end)
            samples = samples_of(data)
            if data.get("sampled") is True:
                log.warning(
                    "%s: iBOS downsampled the trend of %s",
                    self.source.name,
                    fp.reference.object_uuid,
                )
            self._deliver(fp, [s for s in samples if s.timestamp not in fp.delivered])
            if len(samples) < client.page_size:
                break
            if newest_first(data) is False:
                # The oldest came first: the page covers the start of the range.
                if samples[-1].timestamp <= page_start:
                    break
                page_start = samples[-1].timestamp
            else:
                # The newest came first, as the API does: the page covers the end.
                if samples[0].timestamp >= page_end:
                    break
                page_end = samples[0].timestamp

    def _deliver(self, fp: FetchPoint, samples: list[Sample]) -> None:
        sink = self.source.sink
        if fp.value_type is None:
            return
        point = fp.reference.point
        observations: list[Observation] = []
        learned = False
        for sample in samples:
            converted = convert(fp.value_type, sample.value, sample.text)
            if converted.type == "null":
                observations.append(
                    Observation(
                        point, sample.timestamp, "null", reason=converted.reason
                    )
                )
            else:
                observations.append(
                    Observation(
                        point, sample.timestamp, converted.type, converted.value
                    )
                )
            fp.delivered.add(sample.timestamp)
            if fp.last_timestamp is None or sample.timestamp > fp.last_timestamp:
                fp.last_timestamp = sample.timestamp
            if converted.text is not None:
                ordinal, label = converted.text
                table = fp.enum_texts if fp.value_type == "enum" else fp.boolean_texts
                if table.get(ordinal) != label:
                    table[ordinal] = label
                    learned = True
        if sink is not None and observations:
            sink.observations(observations)
        if learned:
            self._send_metadata(fp)

    # --- status --------------------------------------------------------------

    def _reject(self, fp: FetchPoint, reason: str) -> None:
        fp.rejected = True
        self.points.pop(fp.reference.point, None)
        self.source.reject(fp.reference.point, reason)

    def _report(
        self, *, reachable: bool, error: str | None = None, round_done: bool = False
    ) -> None:
        """Tell the status channel at a change, at an error, and at the end of
        a round that succeeded, which is what keeps the last success current."""
        status = self.source.status
        changed = reachable != self.reachable
        self.reachable = reachable
        if status is not None and (changed or error or round_done):
            status.device(self.device, reachable=reachable, error=error)
