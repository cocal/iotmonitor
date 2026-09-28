-- Run monthly on each PostgreSQL database. Keep the same partition names on
-- source and subscriber. Do not drop old partitions before cross-checking.

CREATE TABLE IF NOT EXISTS iot_dlt645_frames_2026_10
PARTITION OF iot_dlt645_frames
FOR VALUES FROM ('2026-10-01 00:00:00+00') TO ('2026-11-01 00:00:00+00');

-- Example for future months:
-- CREATE TABLE iot_dlt645_frames_2026_11
-- PARTITION OF iot_dlt645_frames
-- FOR VALUES FROM ('2026-11-01 00:00:00+00') TO ('2026-12-01 00:00:00+00');
