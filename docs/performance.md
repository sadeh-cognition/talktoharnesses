# Performance reference profile

Release performance gates measure package-owned database and event-delivery work
only. They are regression budgets for the documented CI reference profile, not
latency SLAs for arbitrary hardware, provider networks, database topology, or
workload mix.

## Runner profile

- Ubuntu, Python 3.11
- CI PostgreSQL service and file-backed SQLite
- Seed data through bulk fixture setup outside the timed region
- Five warmups, then 30 samples with `time.perf_counter_ns()`
- Fixed payload sizes on existing public/coarse persistence paths
- Correctness, owner isolation, order, and cap behavior asserted before timing
- Query counts recorded; N+1 increases fail even when wall-clock budgets pass

Do not benchmark provider response time, network latency, model generation,
package installation, or process startup.

## Budgets (first stable release)

| Operation | Fixed dataset | Gate |
| --- | --- | --- |
| Accept an idempotent submit command | Existing conversation, no provider delivery | p95 ≤ 250 ms |
| List one conversation-shell page | 10,000 owner-scoped conversations, page size 50 | p95 ≤ 250 ms |
| Search one conversation page | 10,000 indexed owner-scoped documents, two terms, page size 50 | p95 ≤ 500 ms |
| Replay committed events | 5,000 events totaling no more than 5 MiB | p95 ≤ 2 s |
| Deliver a committed event to a connected SSE consumer | One local producer/consumer | p95 ≤ 250 ms PostgreSQL; ≤ 500 ms SQLite |

If a target fails, optimize the existing query/index/batching path and rerun
parity tests. Do not add caches, denormalized projections, settings, background
indexes, or API variants unless the measured failure proves the gate cannot be
met within the existing contract.
