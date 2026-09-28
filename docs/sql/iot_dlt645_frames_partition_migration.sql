-- Controlled migration for iot_dlt645_frames.
-- Run during a maintenance window on BOTH databases, with the subscription
-- disabled and the ingress API stopped. This script is intentionally not
-- executed automatically because it replaces an online replicated table.

BEGIN;

CREATE TABLE iot_dlt645_frames_partitioned (
    id BIGINT NOT NULL,
    source_id TEXT NOT NULL,
    event_id TEXT NOT NULL,
    request_id TEXT NOT NULL,
    site_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    measurement_point_id TEXT NOT NULL,
    protocol TEXT NOT NULL,
    sequence BIGINT NOT NULL,
    captured_at TIMESTAMPTZ NOT NULL,
    direction TEXT NOT NULL,
    frame_hex TEXT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    metric_key TEXT,
    metric_value DOUBLE PRECISION,
    metric_unit TEXT,
    raw_archive_path TEXT NOT NULL,
    PRIMARY KEY (captured_at, id)
) PARTITION BY RANGE (captured_at);

CREATE TABLE iot_dlt645_frames_2026_08 PARTITION OF iot_dlt645_frames_partitioned
    FOR VALUES FROM ('2026-08-01 00:00:00+00') TO ('2026-09-01 00:00:00+00');
CREATE TABLE iot_dlt645_frames_2026_09 PARTITION OF iot_dlt645_frames_partitioned
    FOR VALUES FROM ('2026-09-01 00:00:00+00') TO ('2026-10-01 00:00:00+00');
CREATE TABLE iot_dlt645_frames_default PARTITION OF iot_dlt645_frames_partitioned DEFAULT;

INSERT INTO iot_dlt645_frames_partitioned
SELECT id, source_id, event_id, request_id::text, site_id, device_id,
       measurement_point_id, protocol, sequence, captured_at, direction,
       frame_hex, received_at, metric_key, metric_value, metric_unit,
       raw_archive_path
FROM iot_dlt645_frames;

ALTER TABLE iot_dlt645_frames RENAME TO iot_dlt645_frames_legacy;
ALTER TABLE iot_dlt645_frames_partitioned RENAME TO iot_dlt645_frames;

CREATE INDEX iot_dlt645_frames_received_v2_idx ON iot_dlt645_frames (received_at DESC);
CREATE INDEX iot_dlt645_frames_metric_time_v2_idx ON iot_dlt645_frames (metric_key, captured_at DESC);
ALTER TABLE iot_dlt645_frames REPLICA IDENTITY FULL;

COMMIT;

-- After validation, and only after the new subscription is confirmed healthy:
-- DROP TABLE iot_dlt645_frames_legacy;
