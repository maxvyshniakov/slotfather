# Naming and compatibility

Definitions live in [the glossary](../GLOSSARY.md). This document records naming
choices and compatibility aliases; it does not define a second vocabulary.

## Decisions

| Existing name | Canonical name | Reason |
| --- | --- | --- |
| lane, task, job for a queue row | request | Slotfather schedules requests from arbitrary callers. A caller workflow lane is one possible source, not the scheduled object. |
| active request for a held request | running request | Waiting requests are also live; running matches the displayed state. |
| lease used for submitted work | request | A lease is the permission held by a request, not the work or its process tree. |
| unleased cores | unleased load | The metric estimates demand outside leases, not unused or measured CPU cores. |
| loaded … of … cores | system load | Load average and CPU utilization are different observations. The display presents the load value and core count separately. |
| `backfill by` alongside `start est` | `start est` | Both displayed the same instant; the backfill constraint is explained with that estimate. |
| job count for command parallelism | worker budget | Internal command parallelism is different from a request's slot budget. |
| class for scheduling | priority | Priority is request state, not arbitrary caller metadata. |
| guard for the memory condition | memory block | The condition has a specific cause and affects additional grants; it is not a general protection mechanism. |

The dashboard uses REQUESTS and REQUEST DETAILS. The `label` field names a
request; it is not another entity. `pass until` and `start est` remain the concise display labels for the distinct scheduling times explained
in [dashboard help](dashboard.md).

An owner with a held lease may be called a holder in scheduling code; a waiter
is a waiting request. These describe roles, not additional domain objects.
A worker executes part of a command and is not a request, slot or caller lane.
Operating-system jobs, processes and threads retain their OS meanings.

## Relationship to lanefather

lanefather owns a repository workflow lane: claims, checks, review and landing.
Slotfather owns requests for machine capacity. Their lifetimes and identities differ:
a lane can make several requests, and a request can come from a shell or CI without
any lane. A lane can also remain open while none of its work holds slots.

`meta.lane` may associate a request with its caller's lane. It is caller metadata,
not a second name for the Slotfather request. Labels remain display names and do
not establish a one-to-one relationship. This distinction also applies to callers
other than Straylight; lanefather is a separate reusable workflow engine.

## Compatibility aliases

The current request format has one representation; the unrelated surfaces below retain their meanings:

- `available` reports queue space; it does not apply memory policy. Dashboard
  `admission` reports admission capacity, which includes memory checks.
- `advise` recommends a worker budget. Its JSON field `jobs` retains its name;
  `memory_short` remains the wire field for a memory block.
- `entries`, `held`, `free`, `legacy`, `lease`, `label` and the other
  published JSON/state keys retain their documented meanings and spellings.
- `held` / `waiting` are storage states; the dashboard presents held work as
  running. Historical event verbs retain their original spellings.
- Caller metadata such as `meta.lane`, labels such as `lane-644-build-once`,
  captured command arguments and recorded historical data retain their source
  names. They are not Slotfather synonyms for request.
- Version-1 demo metrics retain the recorded `lanes` collection key. The replay
  reads that collection as request observations without rewriting measurements.

Internal presentation identifiers use requests and memory blocks. Historical
review reports, fixtures from old versions and measurement artifacts describe
the versions they recorded; they are not renamed to suggest newer behavior.

## Structured request context

`priority` is a top-level request field, defaulting to `normal`; its values are
`interactive`, `normal` and `batch`. `ticket` is an optional top-level identifier.
CLI callers use `--priority` and `--ticket`; library callers use the corresponding
keyword arguments. Priority and ticket are not accepted as metadata.

The retired `--class`, `meta.class`, `meta.priority` and `meta.ticket` inputs are
rejected. Request JSON uses version 2 with `priority` and optional `ticket`;
there is no duplicate `class` field. Installation requires old clients to drain
and converts persisted history once, outside ordinary reads. No runtime fallback
interprets the retired fields.

The dashboard shows known caller fields individually: phase, kind, check,
revision (`sha`), run on (`on`) and session. The remaining custom keys stay in
`meta`. The lanefather state path is a lookup reference rather than display data.
The lane lifecycle is labelled `stage`; `phase` belongs to the execution. The
saved verification result is labelled `receipt`. Version-1 lanefather response
keys `status` and `checks` retain their compatibility spellings.
