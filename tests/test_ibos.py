"""The iBOS source's reference resolution and its vocabulary: typing at
activation and what the declaration says about it."""

from __future__ import annotations

from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

import pytest
from bricklogger.sdk import GraphReader
from bricklogger.sdk.testing import graph_from_turtle
from pyoxigraph import RdfFormat, Store

from bricklogger_ibos.declaration import NAMESPACE, SOURCE, VOCABULARY
from bricklogger_ibos.references import resolve_references

EX = "https://example.com/bldg#"
REF = "https://brickschema.org/schema/Brick/ref#"

MODEL = """\
@prefix brick: <https://brickschema.org/schema/Brick#> .
@prefix ref: <https://brickschema.org/schema/Brick/ref#> .
@prefix ibos: <https://brick.cx2.dk/schema/ibos#> .
@prefix bacnet: <http://data.ashrae.org/bacnet/2020#> .
@prefix ex: <https://example.com/bldg#> .

ex:Building_A a brick:Building, ibos:Project ;
    ibos:project-id 4711 ;
    ibos:project-uuid "0D3F5C2E-6A1B-4C8F-9E2D-7B5A1C3D4E6F" .
ex:Site_B a brick:Site ; ibos:project-id 4712 .
ex:Nameless a brick:Building .
ex:Ctrl a bacnet:BACnetDevice ; bacnet:device-instance 1201 .

ex:SAT a brick:Supply_Air_Temperature_Sensor ;
    ref:hasExternalReference [
        a ibos:Reference ;
        ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afa6" ;
        ibos:project ex:Building_A ;
        ibos:object-type "Analog_Input" ;
        ibos:object-instance 3 ;
        ibos:object-name "AHU01_SAT" ;
        ibos:device-uuid "9b2c7d1e-4f3a-4b5c-8d6e-1a2b3c4d5e6f" ] .
ex:Untyped a brick:Zone_Air_Temperature_Sensor ;
    ref:hasExternalReference [
        ibos:object-uuid "{3FA85F64-5717-4562-B3FC-2C963F66AFA7}" ;
        ibos:project ex:Site_B ] .
ex:Malformed a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        ibos:object-uuid "not-a-uuid" ; ibos:project ex:Building_A ] .
ex:NoProject a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afa8" ] .
ex:NoProjectId a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afa9" ;
        ibos:project ex:Nameless ] .
ex:BadInstance a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afaa" ;
        ibos:project ex:Building_A ; ibos:object-instance "three" ] .
ex:TypedOnly a brick:Temperature_Sensor ;
    ref:hasExternalReference [ a ibos:Reference ; ibos:object-name "SAT" ] .
ex:Twice a brick:Temperature_Sensor ;
    ref:hasExternalReference
        [ ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afab" ;
          ibos:project ex:Building_A ] ,
        [ ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afac" ;
          ibos:project ex:Building_A ] .
ex:Preferred a brick:Temperature_Sensor ;
    ref:hasExternalReference
        [ ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afab" ;
          ibos:project ex:Building_A ; ref:preferred true ] ,
        [ ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afac" ;
          ibos:project ex:Building_A ] .
ex:Both a brick:Temperature_Sensor ;
    ref:hasExternalReference
        [ bacnet:object-identifier "analog-input,3" ; bacnet:objectOf ex:Ctrl ] ,
        [ ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afad" ;
          ibos:project ex:Building_A ] .
ex:BACnetOnly a brick:Temperature_Sensor ;
    ref:hasExternalReference [
        bacnet:object-identifier "analog-input,4" ; bacnet:objectOf ex:Ctrl ] .
"""

IBOS_POINTS = {
    f"{EX}{name}"
    for name in (
        "SAT",
        "Untyped",
        "Malformed",
        "NoProject",
        "NoProjectId",
        "BadInstance",
        "TypedOnly",
        "Twice",
        "Preferred",
        "Both",
    )
}


class StoreReader:
    """A GraphReader over a plain Oxigraph store, without inference."""

    def __init__(self, turtle: str) -> None:
        self.store = Store()
        self.store.load(turtle.encode(), RdfFormat.TURTLE)
        self.prefixes = {"ex": EX}

    def query(self, sparql: str) -> Any:
        return self.store.query(sparql, use_default_graph_as_union=True)


def test_reference_resolution_and_rejections() -> None:
    resolved = resolve_references(StoreReader(MODEL))
    sat = resolved.references[f"{EX}SAT"]
    assert sat.object_uuid == "3fa85f64-5717-4562-b3fc-2c963f66afa6"
    assert (sat.project, sat.project_id) == (f"{EX}Building_A", 4711)
    assert sat.project_uuid == "0d3f5c2e-6a1b-4c8f-9e2d-7b5a1c3d4e6f"
    assert (sat.object_type, sat.object_instance) == ("analog-input", 3)
    assert sat.object_name == "AHU01_SAT"
    assert sat.device_uuid == "9b2c7d1e-4f3a-4b5c-8d6e-1a2b3c4d5e6f"
    untyped = resolved.references[f"{EX}Untyped"]
    assert untyped.object_uuid == "3fa85f64-5717-4562-b3fc-2c963f66afa7"
    assert (untyped.project_id, untyped.project_uuid) == (4712, None)
    assert (untyped.object_type, untyped.object_instance) == (None, None)
    assert resolved.references[f"{EX}Preferred"].object_uuid.endswith("afab")
    assert resolved.references[f"{EX}Both"].object_uuid.endswith("afad")
    assert f"{EX}BACnetOnly" not in resolved.references
    assert f"{EX}BACnetOnly" not in resolved.problems

    problems = {uri.split("#")[1]: p for uri, p in resolved.problems.items()}
    assert set(problems) == {
        "Malformed",
        "NoProject",
        "NoProjectId",
        "BadInstance",
        "TypedOnly",
        "Twice",
    }
    assert "malformed object uuid" in problems["Malformed"].reason
    assert "names no project" in problems["NoProject"].reason
    assert "no project-id" in problems["NoProjectId"].reason
    assert "malformed object-instance" in problems["BadInstance"].reason
    assert problems["BadInstance"].project_id == 4711
    assert "no object-uuid" in problems["TypedOnly"].reason
    assert "none is preferred" in problems["Twice"].reason


@pytest.fixture(scope="module")
def activated(tmp_path_factory: pytest.TempPathFactory) -> GraphReader:
    """The model activated with the iBOS vocabulary loaded."""
    directory: Path = tmp_path_factory.mktemp("data")
    return graph_from_turtle(MODEL, directory, vocabularies=(VOCABULARY,))


def _values(graph: GraphReader, sparql: str, variable: str) -> set[str]:
    return {str(row[variable].value) for row in graph.query(sparql)}


def test_the_vocabulary_types_references_and_projects_at_activation(
    activated: GraphReader,
) -> None:
    typed = _values(
        activated,
        f"SELECT ?p WHERE {{ ?p <{REF}hasExternalReference> ?r . "
        f"?r a <{NAMESPACE}Reference> }}",
        "p",
    )
    assert typed == IBOS_POINTS, "typed or not, every node with an object-uuid"
    external = _values(
        activated,
        f"SELECT ?p WHERE {{ ?p <{REF}hasExternalReference> ?r . "
        f"?r a <{NAMESPACE}Reference> . ?r a <{REF}ExternalReference> }}",
        "p",
    )
    assert external == IBOS_POINTS, "the subclass axiom reaches the inferred graph"
    projects = _values(
        activated, f"SELECT ?x WHERE {{ ?x a <{NAMESPACE}Project> }}", "x"
    )
    assert projects == {f"{EX}Building_A", f"{EX}Site_B", f"{EX}Nameless"}
    assert activated.prefixes["ibos"] == NAMESPACE


def test_the_declaration_carries_the_vocabulary() -> None:
    assert SOURCE.vocabulary is VOCABULARY
    assert SOURCE.reference_types == ("ibos:Reference",)
    assert (VOCABULARY.prefix, VOCABULARY.namespace) == ("ibos", NAMESPACE)
    assert "ibos:Reference a owl:Class" in VOCABULARY.text()


def test_the_plugin_is_found_through_its_entry_point() -> None:
    found = {
        point.name: point.value for point in entry_points(group="bricklogger.sources")
    }
    assert found["ibos"] == "bricklogger_ibos.declaration:SOURCE"
