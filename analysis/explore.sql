-- Results describe recorded test samples, not time-weighted availability.

-- Quality accounting: no observation disappears from this denominator.
SELECT bandwidth_status, latency_status, count(*) AS samples
FROM observations
GROUP BY bandwidth_status, latency_status
ORDER BY samples DESC;

-- Compare within client and endpoint; a changing endpoint mix can change averages.
SELECT substr(timestamp_local, 1, 7) AS month, client, server_id,
       server_name, server_location, count(*) AS tests,
       round(avg(download_mbps), 1) AS mean_download_mbps,
       round(avg(upload_mbps), 1) AS mean_upload_mbps,
       sum(latency_status != 'valid') AS tests_with_invalid_latency
FROM throughput_samples
GROUP BY month, client, server_id, server_name, server_location
HAVING count(*) >= 20
ORDER BY month, tests DESC;

-- DNS episodes can be investigated separately from routing and timeout evidence.
SELECT substr(timestamp_local, 1, 10) AS day, failure_kind, count(*) AS events
FROM connection_errors
GROUP BY day, failure_kind
ORDER BY events DESC, day
LIMIT 20;

-- Preserve and inspect the suspected schedule/tool interaction.
SELECT substr(timestamp_local, 15, 2) AS minute,
       count(*) AS recorded_tests,
       sum(CASE WHEN failure_kind = 'server_selection_failed' THEN 1 ELSE 0 END)
           AS server_selection_failures
FROM observations
WHERE timestamp_local >= '2026-01-01' AND timestamp_local < '2026-06-01'
GROUP BY minute
ORDER BY minute;

-- Example audit trail for a changed or recovered result.
SELECT source_rowid, timestamp_local, original_download, download_mbps,
       bandwidth_status, original_error, source_log_line
FROM observations
WHERE bandwidth_status IN ('corrected_x8', 'recovered_from_log')
ORDER BY source_rowid
LIMIT 5;
