-- Signal's canonical working schema. The database is a build artifact;
-- SQLite snapshots remain immutable provenance inputs.

CREATE TABLE IF NOT EXISTS source_metadata (
    source_name VARCHAR PRIMARY KEY,
    source_path VARCHAR NOT NULL,
    sha256 VARCHAR,
    row_count BIGINT,
    captured_utc TIMESTAMPTZ,
    notes VARCHAR
);

CREATE TABLE IF NOT EXISTS speedtest_attempts (
    attempt_id VARCHAR PRIMARY KEY,
    run_id VARCHAR NOT NULL,
    observed_at_utc TIMESTAMPTZ,
    observed_at_local TIMESTAMP,
    attempt_no INTEGER NOT NULL,
    requested_server_id VARCHAR,
    server_id VARCHAR,
    server_name VARCHAR,
    server_location VARCHAR,
    server_country VARCHAR,
    status VARCHAR NOT NULL,
    failure_kind VARCHAR,
    error_message VARCHAR,
    download_mbps DOUBLE,
    upload_mbps DOUBLE,
    latency_ms DOUBLE,
    jitter_ms DOUBLE,
    packet_loss_pct DOUBLE,
    client_version VARCHAR,
    command VARCHAR,
    raw_json VARCHAR,
    source_name VARCHAR NOT NULL,
    source_rowid BIGINT
);

CREATE VIEW IF NOT EXISTS speedtest_results AS
SELECT * FROM speedtest_attempts WHERE status = 'success';

CREATE TABLE IF NOT EXISTS pihole_domains (
    domain_id INTEGER PRIMARY KEY,
    domain VARCHAR
);

CREATE TABLE IF NOT EXISTS pihole_clients (
    client_id INTEGER PRIMARY KEY,
    ip VARCHAR,
    name VARCHAR
);

CREATE TABLE IF NOT EXISTS pihole_forwarders (
    forward_id INTEGER PRIMARY KEY,
    forwarder VARCHAR
);

CREATE TABLE IF NOT EXISTS pihole_status_codes (
    code INTEGER PRIMARY KEY,
    label VARCHAR,
    class VARCHAR
);

CREATE TABLE IF NOT EXISTS pihole_network (
    network_id INTEGER PRIMARY KEY,
    hwaddr VARCHAR,
    interface VARCHAR,
    first_seen_epoch BIGINT,
    last_query_epoch BIGINT,
    num_queries BIGINT,
    mac_vendor VARCHAR,
    aliasclient_id INTEGER
);

CREATE TABLE IF NOT EXISTS pihole_network_addresses (
    network_id INTEGER,
    ip VARCHAR PRIMARY KEY,
    last_seen_epoch BIGINT,
    name VARCHAR,
    name_updated_epoch BIGINT
);

CREATE TABLE IF NOT EXISTS pihole_queries (
    query_id BIGINT PRIMARY KEY,
    timestamp_epoch DOUBLE,
    query_type INTEGER,
    status INTEGER,
    domain_id INTEGER,
    client_id INTEGER,
    forward_id INTEGER,
    reply_type INTEGER,
    reply_time_s DOUBLE,
    dnssec INTEGER,
    list_id INTEGER,
    ede INTEGER
);

CREATE VIEW IF NOT EXISTS pihole_queries_enriched AS
SELECT
    q.query_id,
    timezone('America/Denver', to_timestamp(q.timestamp_epoch)) AS timestamp_local,
    q.timestamp_epoch,
    q.status,
    COALESCE(s.label, 'Unknown') AS status_label,
    COALESCE(s.class, 'unknown') AS status_class,
    COALESCE(s.class = 'blocked', false) AS is_blocked,
    COALESCE(s.class = 'allowed_forwarded', false) AS is_forwarded,
    d.domain,
    c.ip AS client_ip,
    c.name AS client_name,
    na.name AS network_name,
    n.mac_vendor,
    f.forwarder
FROM pihole_queries q
LEFT JOIN pihole_domains d ON d.domain_id = q.domain_id
LEFT JOIN pihole_clients c ON c.client_id = q.client_id
LEFT JOIN pihole_network_addresses na ON na.ip = c.ip
LEFT JOIN pihole_network n ON n.network_id = na.network_id
LEFT JOIN pihole_forwarders f ON f.forward_id = q.forward_id
LEFT JOIN pihole_status_codes s ON s.code = q.status;

CREATE VIEW IF NOT EXISTS pihole_daily AS
SELECT
    CAST(timestamp_local AS DATE) AS local_date,
    count(*) AS total_queries,
    count(DISTINCT client_ip) AS clients,
    count(DISTINCT domain) AS domains,
    count(*) FILTER (WHERE is_blocked) AS blocked_queries,
    count(*) FILTER (WHERE is_forwarded) AS forwarded_queries
FROM pihole_queries_enriched
GROUP BY 1
ORDER BY 1;

CREATE VIEW IF NOT EXISTS pihole_domain_monthly AS
SELECT
    CAST(date_trunc('month', timestamp_local) AS DATE) AS month,
    domain,
    count(*) AS total_queries,
    count(DISTINCT client_ip) AS clients,
    count(*) FILTER (WHERE is_blocked) AS blocked_queries,
    count(*) FILTER (WHERE is_forwarded) AS forwarded_queries
FROM pihole_queries_enriched
WHERE domain IS NOT NULL
GROUP BY 1, 2;

CREATE VIEW IF NOT EXISTS pihole_client_monthly AS
SELECT
    CAST(date_trunc('month', timestamp_local) AS DATE) AS month,
    client_ip,
    max(client_name) AS client_name,
    max(mac_vendor) AS mac_vendor,
    count(*) AS total_queries,
    count(DISTINCT domain) AS domains,
    count(*) FILTER (WHERE is_blocked) AS blocked_queries,
    count(*) FILTER (WHERE is_forwarded) AS forwarded_queries
FROM pihole_queries_enriched
WHERE client_ip IS NOT NULL
GROUP BY 1, 2;

CREATE VIEW IF NOT EXISTS speedtest_dns_windows AS
SELECT
    s.attempt_id,
    s.observed_at_utc,
    s.failure_kind,
    count(p.query_id) AS nearby_queries,
    count(DISTINCT p.client_ip) AS nearby_clients,
    count(DISTINCT p.domain) AS nearby_domains
FROM speedtest_attempts s
LEFT JOIN pihole_queries_enriched p
    ON p.timestamp_local BETWEEN COALESCE(timezone('America/Denver', s.observed_at_utc), s.observed_at_local) - INTERVAL 2 MINUTE
                             AND COALESCE(timezone('America/Denver', s.observed_at_utc), s.observed_at_local) + INTERVAL 2 MINUTE
WHERE s.failure_kind IS NOT NULL
GROUP BY 1, 2, 3;
