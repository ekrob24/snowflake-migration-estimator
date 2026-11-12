-- Google BigQuery job-history export for sfestimate (`--source bigquery`).
--
-- Produces one row per job with the columns parsers.parse_bigquery expects:
--   job_id, creation_time, start_time, end_time, user_email, job_type,
--   statement_type, total_bytes_processed, total_slot_ms, cache_hit,
--   state, error_result
--
-- REGION: `region-us` below must match the region your datasets live in, e.g.
-- `region-eu`, `region-europe-west2`, `region-asia-northeast1`. Run it once per
-- region if you use several, and concatenate the CSVs.
--
-- SCOPE: INFORMATION_SCHEMA.JOBS covers the current project. For every project
-- in an organization use INFORMATION_SCHEMA.JOBS_BY_ORGANIZATION (same columns,
-- needs organization-level permissions). You need bigquery.jobs.listAll on the
-- project (e.g. the BigQuery Resource Viewer role) to see other users' jobs.
--
-- error_result is a STRUCT in BigQuery; it is flattened to its `reason` string
-- (empty when the job succeeded) so the CSV has a single scalar column.
--
-- How to run and export to CSV:
--
--   bq CLI:
--     bq query --use_legacy_sql=false --format=csv --max_rows=100000000 \
--        "$(cat export_queries/bigquery.sql)" > bigquery_history.csv
--     (Timestamps may come out as "YYYY-MM-DD HH:MM:SS[.ffffff] UTC" or ISO 8601;
--      the parser is meant to accept both. Check the first lines of the file.)
--
--   Large histories: save the result to a table, then
--     bq extract --destination_format=CSV <project>:<dataset>.<table> gs://bucket/bigquery_history_*.csv
--
--   Console: run the query, then "Save results" -> CSV (limited to small results)
--   or "Save results" -> BigQuery table and extract as above.

SELECT
    job_id,
    creation_time,
    start_time,
    end_time,
    user_email,
    job_type,
    statement_type,
    total_bytes_processed,
    total_slot_ms,
    cache_hit,
    state,
    COALESCE(error_result.reason, '') AS error_result
FROM `region-us`.INFORMATION_SCHEMA.JOBS
-- creation_time is the partitioning column; filtering on it keeps the scan cheap.
WHERE creation_time >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 14 DAY)
  AND job_type = 'QUERY'
ORDER BY start_time;
