"""Resolving a point's iBOS reference from the graph.

The reference node carries ``ibos:object-uuid`` and ``ibos:project``, a link
to a node with ``ibos:project-id``; ``object-type``, ``object-instance``,
``object-name`` and ``device-uuid`` are informational and checked against the
API in operation. A reference the source recognises but cannot use gives the
point the outcome ``rejected`` with a reason. See
``README.md``, "The reference".
"""

from __future__ import annotations

import uuid
from collections import defaultdict
from dataclasses import dataclass
from typing import Any

from bricklogger.sdk.contract import GraphReader
from pyoxigraph import BlankNode, Literal, NamedNode, QuerySolutions

from bricklogger_ibos.vocabulary import NAMESPACE

REF = "https://brickschema.org/schema/Brick/ref#"
RDF_TYPE = "http://www.w3.org/1999/02/22-rdf-syntax-ns#type"

Description = dict[str, list[Any]]
"""A node's triples: property IRI to the values in the graph."""


@dataclass(frozen=True)
class IBOSReference:
    """A resolved reference: the object to fetch and the project it belongs to."""

    point: str
    object_uuid: str
    project: str
    project_id: int
    project_uuid: str | None = None
    object_type: str | None = None
    object_instance: int | None = None
    object_name: str | None = None
    device_uuid: str | None = None


@dataclass(frozen=True)
class ReferenceProblem:
    """Why a point's reference cannot be used; the project when it resolved."""

    point: str
    reason: str
    project_id: int | None = None
    project_uuid: str | None = None


@dataclass
class ResolvedReferences:
    references: dict[str, IBOSReference]
    problems: dict[str, ReferenceProblem]


def resolve_references(graph: GraphReader) -> ResolvedReferences:
    """Read every iBOS reference in the graph and resolve or reject it."""
    nodes = _describe_reference_nodes(graph)
    projects = _describe_projects(graph)
    by_point: dict[str, list[Description]] = defaultdict(list)
    for (point, _), description in nodes.items():
        by_point[point].append(description)

    resolved = ResolvedReferences({}, {})
    for point, candidates in by_point.items():
        ibos = [c for c in candidates if _term(c, "object-uuid") is not None]
        if not ibos:
            if any(_is_typed(c, NAMESPACE + "Reference") for c in candidates):
                resolved.problems[point] = ReferenceProblem(
                    point, "the reference has no object-uuid"
                )
            continue
        if len(ibos) > 1:
            preferred = [c for c in ibos if _is_true(_term(c, REF + "preferred"))]
            if len(preferred) != 1:
                resolved.problems[point] = ReferenceProblem(
                    point,
                    "the point has several iBOS references and none is preferred",
                )
                continue
            ibos = preferred
        outcome = _resolve_one(point, ibos[0], projects)
        if isinstance(outcome, IBOSReference):
            resolved.references[point] = outcome
        else:
            resolved.problems[point] = outcome
    return resolved


def _resolve_one(
    point: str, description: Description, projects: dict[str, Description]
) -> IBOSReference | ReferenceProblem:
    """Resolve one reference; the project comes first, so that a problem with the
    reference still carries the project it belongs to, for the claim."""
    project_node = _term(description, "project")
    project_id: int | None = None
    project_uuid: str | None = None
    project_problem: str | None = None
    if project_node is None:
        project_problem = "the reference names no project"
    else:
        project = projects.get(_term_key(project_node), {})
        id_text = _text(_term(project, "project-id"))
        if id_text is None:
            project_problem = "the project has no project-id"
        else:
            project_id = _integer(id_text)
            if project_id is None:
                project_problem = f"malformed project-id {id_text!r}"
        uuid_given = _text(_term(project, "project-uuid"))
        project_uuid = _uuid(uuid_given) or uuid_given
    uuid_text = _text(_term(description, "object-uuid"))
    object_uuid = _uuid(uuid_text)
    if object_uuid is None:
        return ReferenceProblem(
            point, f"malformed object uuid {uuid_text!r}", project_id, project_uuid
        )
    if project_problem is not None or project_id is None or project_node is None:
        return ReferenceProblem(
            point,
            project_problem or "the reference names no project",
            project_id,
            project_uuid,
        )
    instance_text = _text(_term(description, "object-instance"))
    object_instance = _integer(instance_text)
    if instance_text is not None and object_instance is None:
        return ReferenceProblem(
            point,
            f"malformed object-instance {instance_text!r}",
            project_id,
            project_uuid,
        )
    object_type = _text(_term(description, "object-type"))
    return IBOSReference(
        point=point,
        object_uuid=object_uuid,
        project=_term_key(project_node),
        project_id=project_id,
        project_uuid=project_uuid,
        object_type=(
            object_type.strip().lower().replace("_", "-") if object_type else None
        ),
        object_instance=object_instance,
        object_name=_text(_term(description, "object-name")),
        device_uuid=_text(_term(description, "device-uuid")),
    )


def _uuid(text: str | None) -> str | None:
    """The canonical form of a UUID written in any of the usual ways, or ``None``."""
    if text is None:
        return None
    try:
        return str(uuid.UUID(text.strip()))
    except ValueError:
        return None


def _integer(text: str | None) -> int | None:
    if text is None:
        return None
    stripped = text.strip()
    return int(stripped) if stripped.isdigit() else None


def _term(description: Description, local_or_iri: str) -> Any:
    """The first value of a property given by iBOS local name or by full IRI."""
    iri = local_or_iri if local_or_iri.startswith("http") else NAMESPACE + local_or_iri
    values = description.get(iri)
    return values[0] if values else None


def _text(term: Any) -> str | None:
    if isinstance(term, Literal):
        return term.value
    if isinstance(term, NamedNode):
        return term.value
    return None


def _is_true(term: Any) -> bool:
    return isinstance(term, Literal) and term.value.strip().lower() in ("true", "1")


def _is_typed(description: Description, iri: str) -> bool:
    return any(
        isinstance(t, NamedNode) and t.value == iri
        for t in description.get(RDF_TYPE, [])
    )


def _term_key(term: Any) -> str:
    if isinstance(term, NamedNode):
        return term.value
    if isinstance(term, BlankNode):
        return f"_:{term.value}"
    return str(term)


def _describe_reference_nodes(
    graph: GraphReader,
) -> dict[tuple[str, str], Description]:
    """Every reference node's triples, keyed by (point, reference node)."""
    result = graph.query(
        f"SELECT ?p ?r ?prop ?val WHERE {{ ?p <{REF}hasExternalReference> ?r . "
        "?r ?prop ?val }"
    )
    nodes: dict[tuple[str, str], Description] = {}
    if not isinstance(result, QuerySolutions):
        return nodes
    for solution in result:
        point, node, prop = solution["p"], solution["r"], solution["prop"]
        if not isinstance(point, NamedNode) or not isinstance(prop, NamedNode):
            continue
        description = nodes.setdefault((point.value, _term_key(node)), {})
        description.setdefault(prop.value, []).append(solution["val"])
    return nodes


def _describe_projects(graph: GraphReader) -> dict[str, Description]:
    """Every project node a reference links to, with its triples."""
    result = graph.query(
        f"SELECT ?proj ?prop ?val WHERE {{ ?r <{NAMESPACE}project> ?proj . "
        "?proj ?prop ?val }"
    )
    projects: dict[str, Description] = {}
    if not isinstance(result, QuerySolutions):
        return projects
    for solution in result:
        project, prop = solution["proj"], solution["prop"]
        if not isinstance(prop, NamedNode):
            continue
        description = projects.setdefault(_term_key(project), {})
        description.setdefault(prop.value, []).append(solution["val"])
    return projects
