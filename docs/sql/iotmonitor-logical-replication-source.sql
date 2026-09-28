-- Run on the 192 source database after taking a backup.
-- This migration aligns the local frame table with Monitor Center's replicated schema.
ALTER TABLE iot_dlt645_frames
    ADD COLUMN IF NOT EXISTS source_id TEXT NOT NULL DEFAULT 'iotmonitor-192',
    ADD COLUMN IF NOT EXISTS event_id TEXT,
    ADD COLUMN IF NOT EXISTS raw_archive_path TEXT NOT NULL DEFAULT '';

UPDATE iot_dlt645_frames
SET event_id = COALESCE(event_id, request_id::text || ':' || sequence::text || ':' || direction)
WHERE event_id IS NULL;

ALTER TABLE iot_dlt645_frames
    ALTER COLUMN event_id SET NOT NULL;

CREATE UNIQUE INDEX IF NOT EXISTS iot_dlt645_frames_source_event_uidx
    ON iot_dlt645_frames (source_id, event_id);

CREATE UNIQUE INDEX IF NOT EXISTS iot_dlt645_frames_source_request_seq_dir_uidx
    ON iot_dlt645_frames (source_id, request_id, sequence, direction);

-- Verify before creating the publication:
-- \d+ iot_dlt645_frames
-- The Monitor Center tables must have the same replicated columns and types.
-- For a new publisher, create all four logical-replication objects together.
CREATE PUBLICATION iotmonitor_pub
FOR TABLE
    iot_dlt645_frames,
    iot_meter_frame_metrics,
    iot_frame_stats,
    iot_daily_energy;

-- If iotmonitor_pub already exists, do not rerun CREATE PUBLICATION. Run
-- these ALTER statements instead (each table is added only once):
-- ALTER PUBLICATION iotmonitor_pub ADD TABLE iot_meter_frame_metrics;
-- ALTER PUBLICATION iotmonitor_pub ADD TABLE iot_frame_stats;
-- ALTER PUBLICATION iotmonitor_pub ADD TABLE iot_daily_energy;
