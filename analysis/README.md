# Speedtest analysis dataset

The generated database is `analysis/data/speedtests-clean.db`. It preserves the original snapshot's `speedtests` table, adds a quality/provenance table, and exposes SQL views for different questions. Neither the original snapshot nor production was changed.

## What was corrected

| Treatment | Records |
| --- | ---: |
| Original rows retained exactly | 30,944 |
| Download and upload corrected by ×8, verified against raw JSON | 1,178 |
| Already-correct bandwidth verified against raw JSON | 20,257 |
| Parser-error result recovered from its raw JSON | 1 |
| Latency values invalidated, with bandwidth retained | 1,109 |
| Usable throughput observations | 21,436 |
| Latency observations without known invalid-value markers | 20,327 |

The ×8 correction covers October 11–23, 2025. Matching uses the recorded completion time (a preceding log entry within three seconds) plus both bandwidth directions and ping. It verifies individual records, not just a date-range assumption. Multiple possible matches are withheld from analytical samples rather than guessed. This handles the repeated daylight-saving hour in the actual history. All 21,435 original successes have unique matches, and one parser failure has a unique recoverable result.

The Python speedtest-cli 2.1.3 latency implementation contributes 600,000 ms for each failed probe: it substitutes 3,600 seconds, divides the sum by six, and multiplies by 1,000. There are 1,079 complete-failure sentinels at 1,800,000 ms and 30 partial-failure values near 600,000 or 1,200,000. Their cleaned latency is `NULL`, not zero. The original value and the reason remain available. Bandwidth from those tests remains available, but server selection with failed latency probes may make it less comparable to other tests.

Genuinely low throughput and high latency were not trimmed as outliers: 1,200 measurements below 100 Mbps and 108 latency readings above 1,000 ms remain. “Usable” means supported by the recorded result and free of these known data defects; it is not a guarantee of measurement accuracy. Different clients also have different latency measurement methods.

## Failure evidence is preserved

| Failure category | Records | Interpretation |
| --- | ---: | --- |
| DNS resolution failed | 963 | Resolution failed during the test; root cause unknown |
| No route to host | 12 | Routing/connectivity error reported by the client |
| Network unreachable | 2 | Network-unreachable error reported by the client |
| Request timeout | 10 | Could be path, resolver, remote service or other delay |
| Server selection failed | 8,520 | Uncertain cause; preserve separately from direct connection-error evidence |
| HTTP 502 from test service | 1 | HTTP service/path failure, not evidence of total loss of internet |
| Collector parser error | 1 | Original error preserved; measurements recovered from raw JSON |

No failed test is deleted or automatically declared a proven ISP outage. The half-hour failure pattern alone does not justify erasing those records. Error fractions must not be presented as downtime percentages, and invalid latency measurements must not be counted as separate test attempts.

Redundant Pi-hole resolvers can provide useful comparison points, but
historical records do not identify the resolver queried, whether filtering was
involved, or upstream DNS behavior. DNS failures therefore remain distinct
from WAN/routing evidence.

## Query surfaces

| Table/view | Intended use |
| --- | --- |
| `speedtests` | Unmodified original rows and original error messages |
| `observations` | Every original row, corrected metrics, quality flags and provenance |
| `throughput_samples` | Verified/corrected/recovered Mbps, including tests with invalid latency |
| `latency_samples` | Latency without known invalid markers; throughput and server context included |
| `connection_errors` | 987 DNS/routing/timeout events, explicitly categorized |
| `uncertain_test_failures` | 8,520 server-selection failures, retained for investigation |
| `test_failures` | All 9,509 original errors, including the recovered parser error |
| `analysis_metadata` | Input paths, SHA-256 hashes, rule version and processing summary |

Every derived observation links to its original SQLite `rowid`. Matched results include source log line and timestamp, detected client, server ID/name/location/country, original and corrected values, and correction status. The large raw log remains a separate, preserved source file; it is not duplicated into the database.

Timestamps retain their original local representation. No timezone conversion, interpolation, replacement of absent data with zero, or automatic outage-duration estimation is performed. Missing collection is unknown. Extra late-night samples from the scheduler bug remain flagged by their timestamps and have not been deduplicated; account for uneven sampling when computing time-based averages. The older history that exists only in the log has not been imported.

## Reproduce and verify

From the repository root:

```sh
python3 -m unittest discover -s analysis -v
python3 analysis/clean_speedtests.py --output analysis/data/rebuilt.db
sqlite3 -header -column analysis/data/speedtests-clean.db < analysis/explore.sql
```

The cleaner uses only Python's standard library. It refuses to overwrite any existing output or input file. The output contains the source snapshot via SQLite's backup API; inputs are opened read-only, their hashes are checked, and the output is published only after integrity and row-count checks. Generated data is excluded from Git.

The five tests cover verified unit corrections, retention of slow samples, field-level latency invalidation, preserving connection errors, parser recovery, repeated-hour/ambiguous matches, provenance and refusing overwrites. The actual generated dataset was additionally checked for exact equality of every original row and consistent ×8 corrections.

## Useful first questions

1. **When does performance actually change?** Corrected October and November download medians are 925.7 and 926.1 Mbps; the apparent October step disappears. The later drop survives cleaning. Compare the same client and server before attributing it to the access link.
2. **Are slow periods associated with failed latency probes or changing endpoints?** Retained server IDs and independent quality flags make those comparisons possible.
3. **When does DNS resolution fail?** Several November and December days have 98 DNS-error records. Compare those dates against Pi-hole changes, blocking/upstream settings and any retained resolver logs; test history alone cannot locate the cause.
4. **What explains the exact half-hour failures?** Their uncertain category remains queryable independently from DNS/routing failures and valid measurements.

Primary references: [Ookla units](https://man.freebsd.org/cgi/man.cgi?manpath=FreeBSD+Ports+15.0.quarterly&query=speedtest&sektion=5), [Python speedtest-cli latency calculation](https://github.com/sivel/speedtest-cli/blob/v2.1.3/speedtest.py#L1327-L1396).
