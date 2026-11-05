# Slotfather glossary

One context: scheduling shared work capacity on a machine. Caller workflow lanes
belong to their own context; the relationship is recorded in [terminology](docs/terminology.md#relationship-to-lanefather).

| Term | Meaning |
| --- | --- |
| Slot | One unit of the shared scheduling budget. A slot represents permission to start work, not a hard CPU or memory limit. |
| Capacity | The total slot budget shared by requests on the machine. |
| Request | An attempt to acquire a slot budget for some work. It remains the same request while waiting, running or yielding. |
| Lease | Permission for a request to use an allocated slot budget. A held lease consumes slots; yielding temporarily returns them. |
| Owner | The process responsible for a request and its lease. It may launch the command in a separate process. |
| Command | The executable and arguments supplied for the work. Its process tree can include several processes and workers. |
| Label | The caller-supplied display name of a request. It need not be unique. |
| Metadata | Caller-supplied context attached to a request, such as a ticket or phase. |
| Queue | The requests sharing one scheduling policy and capacity budget, including waiting and running requests. |
| Waiting request | A request that currently holds no slots and is waiting for a grant or to resume after yielding. |
| Running request | A request whose lease currently holds slots. Running describes permission to work, not continuous CPU activity. |
| Command kind | A group of comparable commands used to reuse observed runtime history. It is separate from scheduling priority. |
| Label family | A group of related request labels used to reuse runtime history. |
| Runtime estimate | A prediction of how long work will run, based on comparable completed requests or the scheduling fallback. |
| Queue head | The waiting request whose expected start is protected by the scheduling policy. |
| Priority | A request's scheduling priority: interactive, normal or batch. |
| Overtaking | A later request starting before an earlier waiting request. |
| Overtaking window | The initial period in which later requests that fit may start ahead of a waiting request. |
| Overtaking limit | How long a protected request may wait before later requests in the queue stop passing it. |
| Backfill | Overtaking allowed because later work is predicted to finish before the protected request needs its slots, or uses slots that request will not need. |
| Yield request | A cooperative request for an owner to return slots temporarily so waiting work can start. |
| Reap | Remove a request whose owner is no longer the same live process, returning any slots it held. |
| Yield | Voluntarily returning slots while keeping the request pending until it can resume. |
| Admission capacity | The slot budget a generic new request could receive immediately, after queue order and memory checks. |
| Queue space | The slot budget a generic new request could receive based on queue order, before memory checks. |
| Free slots | Capacity currently unused by held leases. Waiting requests and memory policy can still prevent a new grant. |
| Core budget | The share of machine CPU capacity represented by a request's slots. It is a comparison scale, not a quota. |
| System load | The operating system's load average. Slotfather compares it with the logical CPU count; it is not measured CPU utilization. |
| Unleased load | Estimated system load not accounted for by held slot budgets. It is demand outside leases, not free CPU capacity. |
| Worker budget | A recommended amount of parallel work inside a command. It neither reserves slots nor grants admission. |
| Memory block | A memory, swap or pressure condition that prevents additional grants while requests are running. |
| Idle duration | How long a running request has shown sustained near-zero observed CPU use while keeping its slots. |
| Process identity | A PID together with its process start time, so reuse of a PID does not imply the same process. |
| Session | An active terminal or agent-controlled work context. |
| Sweep | Inspection of leftover processes, optionally stopping only those eligible under the cleanup policy. |
