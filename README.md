# Bricklogger iBOS source

A source plugin for [Bricklogger](https://cx1-aps.github.io/bricklogger/) that collects the trend history of
BACnet objects from the **iBOS Data API**, the cloud service of iBOS
Technologies, for the points a Brick model gives an iBOS reference.

The API exposes the objects of a project's devices, with their trend history,
read-only over HTTPS, authenticated with a personal access token. It carries no
live values — what it serves is each object's trend history — so the source is
a **history source**: its collection method is `poll`, and every round fetches
the samples the cloud has received since the round before, each **with the
cloud's own timestamp**. The objects are the same objects a BACnet/IP source
would read on site, so a point may carry both a BACnet reference and an iBOS
reference; which source collects it follows from the source configuration, and
a point claimed by both is the validation error the
[architecture](https://cx1-aps.github.io/bricklogger/architecture/#which-source-owns-which-point) describes.

## Install

The plugin is installed into Bricklogger's environment, from the CLI, the web
interface's Plugins screen or, in a container, into the plugin volume:

```bash
bricklogger plugins add bricklogger-ibos
sudo systemctl restart bricklogger          # or: bricklogger daemon restart
bricklogger plugins ibos                     # the settings and tools it brings
```

| Plugin | Bricklogger |
|--------|-------------|
| 0.1    | 0.2         |

A plugin is built for one minor version of Bricklogger; after upgrading
Bricklogger across one, upgrade the plugin with the same `plugins add`.

Then put the token in Bricklogger's `env` file and add an instance — guided
with `bricklogger sources add ibos_main --type ibos`, or by hand:

```
IBOS_PAT=...
```

**Coming from Bricklogger 0.1,** where the iBOS source was built in: the
configuration, the type name `ibos`, the vocabulary and the collected history
are unchanged. After the upgrade to Bricklogger 0.2 the instance shows as
failed until this plugin is added and the daemon restarted; it then resumes
from the latest observation of every point.

## Configuration

```yaml
# sources.yaml
ibos_main:
  type: ibos
  token: ${IBOS_PAT}                           # required: personal access token
  url: https://api.data.ibostechnologies.com   # default
  projects: [4711, "0d3f5c2e-6a1b-4c8f-9e2d-7b5a1c3d4e6f"]  # optional: claim scope
  rate_limit: 50                               # default: requests per second
  backfill: 7d                                 # default: history at first assignment
  overlap: 1h                                  # default: window re-read for late samples
  timeout: 30s                                 # default: per request
```

| Key | Requirement | Content |
|-----|-------------|---------|
| `token` | required | The personal access token the instance authenticates with, given as `${IBOS_PAT}` with the value in Bricklogger's [`env` file](https://cx1-aps.github.io/bricklogger/features/configuration/#the-env-file) like every secret |
| `url` | default `https://api.data.ibostechnologies.com` | The API's base URL |
| `projects` | optional | The projects the instance claims: a list of project numbers and UUIDs, matched against each reference's project node |
| `rate_limit` | default `50` | The instance's request budget in requests per second. The API allows 100 per token, and a token may be shared with other clients |
| `backfill` | default `7d` | How far back the first fetch of a point reaches; `0` fetches only what the cloud receives after the assignment |
| `overlap` | default `1h` | How far behind the latest known sample every round starts again, to catch samples the cloud received late |
| `timeout` | default `30s` | How long one request waits for its response |

A reference is in scope when its project's number or UUID is in `projects`.
Without `projects` the instance claims every iBOS reference — the simple
installation configures nothing but the token. The instance declares no
exclusive [resource](https://cx1-aps.github.io/bricklogger/architecture/#isolation-and-resources): two
instances with the same token and different projects are a legitimate way to
split a budget.

## The reference

A point is addressed through Brick's external reference, as for every source:
the point carries `ref:hasExternalReference` to a reference node. Brick's
reference schema has no type for an object in a cloud service, so the
reference is written in the **iBOS vocabulary** described
[below](#the-ibos-vocabulary), namespace `https://brick.cx2.dk/schema/ibos#`
with the prefix `ibos:`:

```turtle
@prefix ibos: <https://brick.cx2.dk/schema/ibos#> .

ex:Building_A a rec:Building, ibos:Project ;
    ibos:project-id 4711 ;
    ibos:project-uuid "0d3f5c2e-6a1b-4c8f-9e2d-7b5a1c3d4e6f" .   # optional

ex:AHU_01_SAT a brick:Supply_Air_Temperature_Sensor ;
    ref:hasExternalReference [
        a ibos:Reference ;
        ibos:object-uuid "3fa85f64-5717-4562-b3fc-2c963f66afa6" ;
        ibos:project ex:Building_A ;
        ibos:object-type "analog-input" ;                        # informational, checked
        ibos:object-instance 3 ;                                 # informational, checked
        ibos:object-name "AHU01_SAT" ;                           # informational
        ibos:device-uuid "9b2c7d1e-4f3a-4b5c-8d6e-1a2b3c4d5e6f"   # informational
    ] .
```

- **The object:** `ibos:object-uuid` is the object's UUID in iBOS, the key
  under which the API serves everything about it. The source recognises an
  iBOS reference by this property, whether or not the node is typed
  `ibos:Reference`: the vocabulary's rule derives the type at activation, as
  Brick's own rules do for Brick's reference types.
- **The project:** `ibos:project` links the reference to the iBOS project the
  object belongs to, a node with `ibos:project-id`, the project's number in
  iBOS. The node is typically the building itself, typed `rec:Building` and
  `ibos:Project` at once; `ibos:project-uuid` and a label are optional. The
  project is the unit by which an instance decides which points it claims.
- **Informational properties:** `ibos:object-type`, in the standard's
  hyphenated spelling, `ibos:object-instance`, `ibos:object-name` and
  `ibos:device-uuid` describe the object as iBOS knows it. They play no part
  in addressing, but the source reads the object's metadata from the API in
  any case, and a type or instance number in the graph that contradicts the
  API's makes the point `rejected`: the reference then almost certainly
  carries the wrong UUID.

**What is rejected.** As for every source, a reference the source recognises
but cannot use gives the point the outcome `rejected` with a reason that
names the problem, and the point is not collected until the model is fixed.
That covers a node typed `ibos:Reference` without `ibos:object-uuid`, a
malformed UUID or instance number, a reference without `ibos:project`, a
project node without `ibos:project-id`, a type or instance number that
contradicts the API, and a point with two iBOS references of which neither is
marked `ref:preferred true`
— where one is marked, it is used. The rule is the same as for BACnet: a
point logged from the wrong object under the right name is worse than a
visible gap.

> [!NOTE]
> **Not a time-series reference.** Brick's `ref:TimeseriesReference` says
> where a point's time series is **stored**. It is reserved for the databases
> Bricklogger writes to and is never a source's address, so an iBOS object is
> not written as one.

## The iBOS vocabulary

The vocabulary is a small extension of Brick's reference schema: one Turtle
document with the classes, properties and rules below, shipped in this
package. The namespace is an identifier; nothing needs to be fetched from it.
The iBOS source [declares it](https://cx1-aps.github.io/bricklogger/architecture/#declaration), so
the daemon loads it at every activation together with Brick's ontology and
rules — a model that uses it needs no network access — and the prefix `ibos:`
is pre-declared like `ref:` and `bacnet:`, in SPARQL and in prefixed names.

| Term | Kind | Content |
|------|------|---------|
| `ibos:Reference` | class, subclass of `ref:ExternalReference` | A reference to an object in iBOS |
| `ibos:Project` | class | An iBOS project; the node may also be the building or site itself |
| `ibos:object-uuid` | string | The object's UUID in iBOS. **Required** |
| `ibos:project` | link | The project the object belongs to. **Required** |
| `ibos:project-id` | integer | The project's number in iBOS, on the project node. **Required** |
| `ibos:project-uuid` | string | The project's UUID, on the project node. Optional |
| `ibos:object-type` | string | The BACnet object type in the standard's hyphenated spelling, e.g. `analog-input`. Informational, checked against the API |
| `ibos:object-instance` | integer | The BACnet instance number. Informational, checked against the API |
| `ibos:object-name` | string | The object's name in iBOS. Informational |
| `ibos:device-uuid` | string | The UUID of the device the object belongs to. Informational |

Two SHACL rules run at activation: a node under `ref:hasExternalReference`
that carries `ibos:object-uuid` is typed `ibos:Reference`, and the object of
`ibos:project` is typed `ibos:Project`.

## Fetching

The source owns the rhythm, within a budget:

- **A round per point.** At the rule's `interval` the source asks the cloud,
  per object, for the samples newer than the latest it has, minus the
  `overlap`. Samples already delivered are dropped in the source, so a
  repeated sample never leaves it; a sample the cloud received late, with a
  timestamp inside the window, is delivered when it appears. The `interval`
  thus bounds how far the copy lags behind the cloud, not the spacing of the
  samples — that is the cloud's, and every sample keeps its own timestamp.
- **Where a point starts.** A point the daemon has never recorded an
  observation for — new in the model, or a fresh runtime state — is fetched
  `backfill` back from that moment. A point it has an observation for — after
  a restart of the daemon or of the instance — is fetched from that latest
  observation minus `overlap`, however long ago, so a restart refetches
  nothing but the overlap and an outage is caught up in pages; the
  [assignment](https://cx1-aps.github.io/bricklogger/architecture/#operation) carries the timestamp. From then
  on only what is new. A point with no new samples in a
  round yields nothing: the age of its last value in status shows that the
  cloud is silent.
- **Pages.** The API serves at most 10 000 samples per request, the newest
  first; a longer stretch — the first fetch, or the catch-up after an outage
  — is read in pages, each reaching further back than the one before, and
  each counting against the budget.
- **The budget.** Requests are spread evenly under `rate_limit`, and nothing
  else is configurable. A round takes at least as long as its requests need —
  3 000 objects at 50 requests per second take a minute — and **rounds never
  pile up**: the next round starts when the previous one has finished, and a
  round that outlasts the interval gives the warning `rate_limited` per
  instance, with the cadence the instance can actually keep. Nothing is lost
  meanwhile, because the cloud keeps the history: the budget costs latency,
  never samples. A `429` from the API is honoured — the source waits the
  `Retry-After` it names — and counted in status.

Capacity follows from the budget: the points an instance serves, each divided
by its interval in seconds, must add up to less than `rate_limit` for the
rounds to keep their intervals — 50 requests per second serve 15 000 points
every five minutes, or 3 000 every minute.

**Timestamps are the cloud's.** Every observation carries the sample's
timestamp as the API gives it, in UTC; the source never stamps a sample
itself. An older sample can therefore arrive after a newer one, and the
daemon's *last known value* is the latest by timestamp, not by arrival, as the
[architecture](https://cx1-aps.github.io/bricklogger/architecture/#the-working-graph) describes.

## Value types and units

An object's type — `object_type` in the API, the BACnet object type — decides
the value type, with the same table as [BACnet/IP](https://cx1-aps.github.io/bricklogger/features/sources/#value-types-and-units).
A sample carries `value`, a number, and `value_text`, a text, and the type
says which of them is the value:

| Object type | Value type | Value and texts |
|-------------|------------|-----------------|
| analog-input, analog-output, analog-value, large-analog-value, loop | `number` | `value` |
| integer-value, positive-integer-value, accumulator | `integer` | `value`, as an integer |
| binary-input, binary-output, binary-value | `boolean` | `value` 1 is true, 0 false; a text in `value_text` that is not the number itself becomes the state's text |
| multi-state-input, multi-state-output, multi-state-value | `enum` | `value` is the state, counting from 1; a text in `value_text` that is not the number itself is learned as the state's text and delivered as metadata as it appears |
| character-string-value | `string` | `value_text` |
| date-time-value | `datetime` | `value_text`, as read; stored in UTC |
| Everything else | — | The point is `rejected`: the datatype has no counterpart in the vocabulary |

A sample whose `value` and `value_text` are both empty is delivered as `null`
with reason `no_value`: the cloud recorded that the collector had no value at
that time, and the time series says so. In the API as observed, `value_text`
repeats the number — `"3"` beside `3.0` — so states stay ordinals.

**Units.** The object's unit comes with its metadata from the API as
`unit_id`, BACnet's engineering-unit number, and as the text `units`, a
symbol such as `°C`, `%` or `m³/h`. The source translates the number into
Brick's unit vocabulary (QUDT) with the same table as BACnet/IP, translates
the symbol where it knows it — the objects list carries the symbol alone —
and otherwise delivers the text as the protocol's own designation. As for
every source, the graph's unit stays primary, a
disagreement gives the `unit_conflict` warning, and values are never
converted.

**When metadata is read.** The object's metadata — type, instance, name and
unit — is read from the API once when a point is assigned. It settles the
value type and the unit, and it is where a graph's `ibos:object-type` or
`ibos:object-instance` is checked; a contradiction gives `rejected`. The
enumeration's texts follow as the states appear in the data.

## Failed requests

The source delivers only what the cloud has recorded. When a request fails,
it delivers **nothing** for that point in that round and fetches the whole
stretch when the next request succeeds, so the time series stays complete and
carries no marks of the logger's own outages. What went wrong shows in
status, per project:

| Answer | Effect |
|--------|--------|
| `404` for the object | The point is `rejected` with the reason that the object is not in iBOS: the reference carries the wrong UUID, or the object is gone |
| `403` for the project | The project is unreachable in status with the error; its points stay `active` and receive nothing until the token has access again. A token's permissions are not a model error |
| `401` | The token is invalid: the instance is `failed` with the error, and the daemon restarts it with backoff |
| `429` | The source waits the `Retry-After` the API names and counts the answer; see [fetching](#fetching) |
| `5xx`, timeout, no connection | The project is unreachable in status until a request succeeds |

This is where a history source differs from a live one: BACnet/IP delivers
`null` with `unreachable` for every failed poll, because nothing else records
that the value was not available, whereas the cloud's history records it
itself.

## Protocol tools

The tools run with `bricklogger sources <instance> <tool>` — through the daemon
when it runs, in-process otherwise — as described for
[protocol tools](https://cx1-aps.github.io/bricklogger/architecture/#protocol-tools) in general. Every tool
returns a structured result that the CLI renders as a table or emits as JSON.

| Tool | Parameters | Result |
|------|------------|--------|
| `projects` | — | The projects the token has access to: number, UUID, name, description and the counts of devices and objects |
| `devices` | `--project`, number or UUID | The project's devices: UUID, device number, name, vendor, model and IP address |
| `objects` | `--device` UUID; `--values` adds the latest sample | The device's objects: UUID, type, instance, name, unit and its QUDT counterpart, optionally the latest sample |
| `read` | `--object` UUID; `--since` (default `1h`) and `--limit` (default `100`) bound the samples | The object's latest samples as the vocabulary sees them — timestamp, type and value — with the raw `value` and `value_text` beside |
| `resolve` | `--point`, a URI in full or prefixed form | How the point's reference resolves — object UUID, project and whether it is in the instance's scope — the object's metadata from the API against the graph's type and instance, and the latest sample, or the problem with the reference |
| `pointlist` | `--project`, number or UUID, optional | The point list: the projects the instance claims, or the one project, with their devices and the objects on each, as one JSON document to keep as a file |

`projects`, `devices`, `objects`, `read` and `pointlist` need nothing but the
instance's configuration, so they also run without a daemon. `resolve` needs
the running daemon, because the working graph is the daemon's. No tool writes
to the cloud; the API is read-only.

### The point list

The point list is what the API has, without a single sample: the projects,
their devices and the objects on each device — an inventory of what the token
can see, to file with the building's documentation or to hand to whoever
builds the Brick model. It is a [document](https://cx1-aps.github.io/bricklogger/architecture/#protocol-tools),
so the CLI writes it as JSON, to standard output or to a file with `-o`:

```
bricklogger sources ibos_main pointlist --project 4711 -o building-a.json
```

In the web interface the point list is offered on the `projects` listing: run
`projects`, and every project row carries a Download for that project's point
list, with a Download for the whole list — every project the instance claims —
above the table. The tool has no form of its own there.

Without `--project` the list covers the projects the instance claims — the
`projects` setting, or every project the token has access to when the setting
is absent. With `--project` it covers that one project, whether or not the
instance claims it. A project the token cannot see fails the tool, as it fails
`devices`.

```json
{
  "instance": "ibos_main",
  "url": "https://api.data.ibostechnologies.com",
  "exported_at": "2026-09-09T13:05:12+00:00",
  "counts": {"projects": 1, "devices": 1, "objects": 2},
  "projects": [
    {
      "number": 4711,
      "uuid": "0d3f5c2e-6a1b-4c8f-9e2d-7b5a1c3d4e6f",
      "name": "Building A",
      "description": null,
      "devices": [
        {
          "uuid": "9b2c7d1e-4f3a-4b5c-8d6e-1a2b3c4d5e6f",
          "number": 1201,
          "name": "Supervisor",
          "vendor": "Tridium",
          "model": "JACE-8000",
          "ip": "192.168.10.20",
          "objects": [
            {"uuid": "3fa85f64-5717-4562-b3fc-2c963f66afa6", "type": "analog-input", "instance": 3, "name": "AHU01_SAT", "unit": "°C", "qudt": "http://qudt.org/vocab/unit/DEG_C"},
            {"uuid": "7c9e6679-7425-40de-944b-e07fc1f90ae7", "type": "binary-input", "instance": 1, "name": "AHU01_FAN", "unit": null, "qudt": null}
          ]
        }
      ]
    }
  ]
}
```

The header names the instance, the API's URL, the time of the export in UTC
and the counts; then follow the projects, each with its devices, each with its
objects. The fields are those of `projects`, `devices` and `objects` — a
project without its two counts, since the lists themselves are there — with
the object type in the standard's hyphenated spelling, so the document reads
as the three lists nested. Building it takes one request per project and one
per device, under the instance's budget as always; a project with thousands of
objects is a handful of requests.

## Reporting a problem

Bugs and questions go in the
[issues](https://github.com/CX1-ApS/bricklogger-ibos/issues). A security
vulnerability is reported privately, as
[SECURITY.md](https://github.com/CX1-ApS/bricklogger-ibos/blob/main/SECURITY.md)
describes.

## License

[MIT](https://github.com/CX1-ApS/bricklogger-ibos/blob/main/LICENSE). iBOS is a
product of iBOS Technologies; this plugin is not affiliated with it.
