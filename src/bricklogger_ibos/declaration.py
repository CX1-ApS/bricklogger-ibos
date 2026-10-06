"""The iBOS source's declaration: what the daemon and the CLI know about it
before an instance exists — its reference type, the vocabulary that defines
it, its configuration schema and its tools. See ``README.md``."""

from __future__ import annotations

from bricklogger.sdk.declaration import (
    SourceDeclaration,
    ToolDeclaration,
    ToolOffer,
)

from bricklogger_ibos.config import DEFAULT_RATE_LIMIT, DEFAULT_URL, IBOSConfig
from bricklogger_ibos.source import IBOSSource
from bricklogger_ibos.tools import (
    DevicesParameters,
    ObjectsParameters,
    PointlistParameters,
    ProjectsParameters,
    ReadParameters,
    ResolveParameters,
)
from bricklogger_ibos.vocabulary import NAMESPACE, VOCABULARY

SOURCE = SourceDeclaration(
    type_name="ibos",
    description="The iBOS Data API: a project's BACnet objects and their trend "
    "history, read from the cloud under a request budget.",
    config_schema=IBOSConfig,
    reference_types=("ibos:Reference",),
    vocabulary=VOCABULARY,
    tools=(
        ToolDeclaration(
            "projects",
            "List the projects the token has access to: number, UUID, name and "
            "the counts of devices and objects.",
            ProjectsParameters,
        ),
        ToolDeclaration(
            "devices",
            "List a project's devices: UUID, device number, name, vendor, model "
            "and IP address.",
            DevicesParameters,
        ),
        ToolDeclaration(
            "objects",
            "List a device's objects: UUID, type, instance, name and unit, and "
            "with --values the latest sample.",
            ObjectsParameters,
        ),
        ToolDeclaration(
            "read",
            "Read an object's latest samples as the vocabulary sees them, with "
            "the API's raw fields beside.",
            ReadParameters,
        ),
        ToolDeclaration(
            "resolve",
            "Show how a point's reference resolves, the object as iBOS knows it "
            "against the graph, and the latest sample.",
            ResolveParameters,
        ),
        ToolDeclaration(
            "pointlist",
            "The point list: the projects the instance claims, or one project, "
            "with their devices and the objects on each, as one JSON document to "
            "keep as a file.",
            PointlistParameters,
            document=True,
            offered_on=ToolOffer("projects", {"project": "number"}),
        ),
    ),
    factory=IBOSSource,
)

__all__ = [
    "DEFAULT_RATE_LIMIT",
    "DEFAULT_URL",
    "NAMESPACE",
    "SOURCE",
    "VOCABULARY",
    "IBOSConfig",
    "IBOSSource",
]
