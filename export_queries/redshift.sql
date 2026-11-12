-- Amazon Redshift query-history export for sfestimate (`--source redshift`).
--
-- Produces one row per query with the columns parsers.parse_redshift expects:
--   query_id, user_id, query_type, status, start_time, end_time,
--   elapsed_time, result_cache_hit, scanned_bytes
--
-- Units:
--   elapsed_time  is in MICROSECONDS (as stored in SYS_QUERY_HISTORY).
--   scanned_bytes is bytes read by scan steps (compressed, columnar), summed per query.
--   start_time / end_time are UTC timestamps.
--
-- Works on Redshift provisioned clusters (patched to a version with SYS_ views)
-- and Redshift Serverless. Run it as a superuser, or as a user granted the
-- SYS:MONITOR role; otherwise SYS_ views only show your own queries.
--
-- Retention: SYS_ monitoring views keep a limited history (roughly 7 days on
-- many clusters; check your environment). If your export covers fewer than
-- 14 days, that's fine: the estimator measures the window from the data.
--
-- How to run and export to CSV:
--
--   psql (works against Redshift):
--     psql "host=<cluster-endpoint> port=5439 dbname=<db> user=<user>" \
--          -v ON_ERROR_STOP=1 --csv -f export_queries/redshift.sql > redshift_history.csv
--     (--csv needs psql 12+; the file contains a single statement.)
--
--   Redshift Query Editor v2: run the query, then "Export" -> CSV.
--
--   Large clusters: UNLOAD the same SELECT to S3 with
--     UNLOAD ('<query below, with single quotes doubled>')
--     TO 's3://bucket/prefix/redshift_history_' IAM_ROLE '<role-arn>'
--     FORMAT CSV HEADER PARALLEL OFF;

WITH scan_bytes AS (
    -- SYS_QUERY_DETAIL has one row per step (scan, hash, aggregate, ...). Summing
    -- input_bytes over every step would count the same data several times, so
    -- only scan steps are included.
    SELECT
        query_id,
        SUM(input_bytes) AS scanned_bytes
    FROM sys_query_detail
    WHERE step_name = 'scan'
      AND start_time >= DATEADD(day, -14, GETDATE())
    GROUP BY query_id
)
SELECT
    h.query_id,
    h.user_id,
    h.query_type,
    h.status,
    h.start_time,
    h.end_time,
    h.elapsed_time,                          -- microseconds
    h.result_cache_hit,
    COALESCE(s.scanned_bytes, 0) AS scanned_bytes
FROM sys_query_history h
LEFT JOIN scan_bytes s
       ON s.query_id = h.query_id
WHERE h.start_time >= DATEADD(day, -14, GETDATE())
  -- user_id 1 is rdsdb, Redshift's internal maintenance user. Its queries
  -- (automatic analyze, vacuum, monitoring) would not migrate to Snowflake.
  AND h.user_id > 1
ORDER BY h.start_time;
