-- Read-only starting points for the unified Signal DuckDB.
-- Run with: duckdb analysis/data/signal.duckdb < analysis/explore_duckdb.sql

-- Source rows and retention windows.
SELECT * FROM source_metadata ORDER BY source_name;
SELECT 'bandwidth' AS source, min(timestamp_local), max(timestamp_local), count(*) FROM bandwidth_observations
UNION ALL
SELECT 'pihole', min(timestamp_local), max(timestamp_local), count(*) FROM pihole_queries_enriched;

-- Pi-hole daily traffic and outcome mix.
SELECT * FROM pihole_daily ORDER BY local_date;

-- Household clients: keep Pi-hole infrastructure separate from device traffic.
SELECT client_ip, client_name, mac_vendor, count(*) AS queries,
       count(*) FILTER (WHERE is_blocked) AS blocked,
       round(100.0 * count(*) FILTER (WHERE is_blocked) / count(*), 1) AS blocked_pct,
       count(DISTINCT domain) AS domains
FROM pihole_queries_enriched
WHERE client_ip NOT IN ('127.0.0.1', '::1')
GROUP BY ALL ORDER BY queries DESC;

-- Domains and provider-facing upstreams.
SELECT domain, count(*) AS queries,
       count(*) FILTER (WHERE is_blocked) AS blocked,
       count(*) FILTER (WHERE is_forwarded) AS forwarded,
       count(*) FILTER (WHERE status_class = 'allowed_cached') AS cached
FROM pihole_queries_enriched GROUP BY domain ORDER BY queries DESC LIMIT 100;
SELECT forwarder, count(*) AS forwarded_queries
FROM pihole_queries_enriched WHERE forwarder IS NOT NULL
GROUP BY forwarder ORDER BY forwarded_queries DESC;

-- The first correlation pass: a two-minute window around each bandwidth DNS failure.
-- A zero nearby_queries result is useful evidence: the query may have failed before
-- reaching the monitored Pi-hole, or the Pi-hole's 91-day retention may not overlap.
SELECT * FROM bandwidth_dns_failure_windows ORDER BY timestamp_local;
