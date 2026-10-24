-- NWM view from the analysis/assimilation series (NOAA is not publishing short_range for this reach right now).
-- Run:  python3 scripts/run_sql.py sql/01c_nwm_view.sql
CREATE SCHEMASTORE VIEW nwm_streamflow WITH CONTAINER water AS
SELECT reach.`reachId` AS reach_id, 'analysis_assimilation' AS series,
       `analysisAssimilation`.series.`referenceTime` AS reference_time,
       `analysisAssimilation`.series.units AS unit,
       d.`validTime` AS valid_time, CAST(d.flow AS DOUBLE) AS flow
FROM noaa_nwm.reach_18229923_analysis_raw
LATERAL VIEW explode(`analysisAssimilation`.series.data) AS d;

SELECT series, unit, COUNT(*) AS n, MIN(valid_time) AS first, MAX(valid_time) AS last, MIN(flow) AS min_flow, MAX(flow) AS max_flow FROM water.nwm_streamflow GROUP BY series, unit;

-- Side by side: USGS observed flow vs NWM simulated flow vs NWS observed flow (hourly, last 48h)
SELECT u.hr,
       ROUND(u.usgs_cfs) AS usgs_cfs,
       ROUND(n.nwm_cfs)  AS nwm_cfs,
       ROUND(w.nws_cfs)  AS nws_cfs
FROM (
    SELECT date_format(to_utc_timestamp(to_timestamp(substr(valid_time, 1, 19)), 'America/Chicago'), 'yyyy-MM-dd HH:00') AS hr,
           AVG(value) AS usgs_cfs
    FROM water.usgs_observed WHERE site_id = '02465000' AND param_code = '00060'
    GROUP BY 1
) u
LEFT JOIN (
    SELECT date_format(to_timestamp(substr(valid_time, 1, 19)), 'yyyy-MM-dd HH:00') AS hr, AVG(flow) AS nwm_cfs
    FROM water.nwm_streamflow GROUP BY 1
) n ON n.hr = u.hr
LEFT JOIN (
    SELECT date_format(to_timestamp(substr(valid_time, 1, 19)), 'yyyy-MM-dd HH:00') AS hr, AVG(flow) * 1000 AS nws_cfs
    FROM water.nwps_stageflow WHERE kind = 'observed' GROUP BY 1
) w ON w.hr = u.hr
ORDER BY u.hr DESC
LIMIT 30;
