-- Fix: NWM short-range data comes from the unfiltered streamflow endpoint.
-- Run:  python3 scripts/run_sql.py sql/01b_fix_nwm.sql

CREATE LIGHTNING REST TABLE reach_18229923_all_raw FROM NOAA_NWM REQUEST(
    endpoint "https://api.water.noaa.gov/nwps/v1/reaches/18229923/streamflow",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE noaa_nwm.reach_18229923_all_raw;

DESCRIBE noaa_nwm.reach_18229923_all_raw;

CREATE SCHEMASTORE VIEW nwm_streamflow WITH CONTAINER water AS
SELECT reach.`reachId` AS reach_id, 'analysis_assimilation' AS series,
       `analysisAssimilation`.series.`referenceTime` AS reference_time,
       `analysisAssimilation`.series.units AS unit,
       d.`validTime` AS valid_time, CAST(d.flow AS DOUBLE) AS flow
FROM noaa_nwm.reach_18229923_analysis_raw
LATERAL VIEW explode(`analysisAssimilation`.series.data) AS d
UNION ALL
SELECT reach.`reachId`, 'short_range',
       `shortRange`.series.`referenceTime`, `shortRange`.series.units,
       d.`validTime`, CAST(d.flow AS DOUBLE)
FROM noaa_nwm.reach_18229923_all_raw
LATERAL VIEW explode(`shortRange`.series.data) AS d;
