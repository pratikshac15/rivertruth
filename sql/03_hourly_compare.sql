-- One hourly UTC view that lines up all four agencies in common units (cfs, ft, mm).
-- Built in Zetaris over the four registered sources. Run:  python3 scripts/run_sql.py sql/03_hourly_compare.sql
CREATE SCHEMASTORE VIEW hourly_compare WITH CONTAINER water AS
SELECT u.hr_utc,
       u.usgs_northport_cfs, u.usgs_northport_peak_cfs, u.usgs_northport_stage_ft, u.usgs_oliver_tailwater_ft,
       n.nwm_cfs,
       w.nws_oliver_cfs, w.nws_oliver_tailwater_ft,
       r.rain_mm
FROM (
    SELECT date_format(to_utc_timestamp(to_timestamp(substr(valid_time, 1, 19)), 'America/Chicago'), 'yyyy-MM-dd HH:00') AS hr_utc,
           ROUND(AVG(CASE WHEN site_id = '02465000' AND param_code = '00060' THEN value END), 1) AS usgs_northport_cfs,
           MAX(CASE WHEN site_id = '02465000' AND param_code = '00060' THEN value END)            AS usgs_northport_peak_cfs,
           ROUND(AVG(CASE WHEN site_id = '02465000' AND param_code = '00065' THEN value END), 2) AS usgs_northport_stage_ft,
           ROUND(AVG(CASE WHEN site_id = '02465005' AND param_code = '00065' THEN value END), 2) AS usgs_oliver_tailwater_ft
    FROM water.usgs_observed
    GROUP BY 1
) u
LEFT JOIN (
    SELECT date_format(to_timestamp(substr(valid_time, 1, 19)), 'yyyy-MM-dd HH:00') AS hr_utc, ROUND(AVG(flow), 1) AS nwm_cfs
    FROM water.nwm_streamflow GROUP BY 1
) n ON n.hr_utc = u.hr_utc
LEFT JOIN (
    SELECT date_format(to_timestamp(substr(valid_time, 1, 19)), 'yyyy-MM-dd HH:00') AS hr_utc,
           ROUND(AVG(flow) * 1000, 1) AS nws_oliver_cfs, ROUND(AVG(stage), 2) AS nws_oliver_tailwater_ft
    FROM water.nwps_stageflow WHERE kind = 'observed' GROUP BY 1
) w ON w.hr_utc = u.hr_utc
LEFT JOIN (
    SELECT date_format(to_timestamp(substr(valid_time, 1, 19)), 'yyyy-MM-dd HH:00') AS hr_utc, ROUND(SUM(precip), 2) AS rain_mm
    FROM water.rain_hourly GROUP BY 1
) r ON r.hr_utc = u.hr_utc;

SELECT * FROM water.hourly_compare ORDER BY hr_utc DESC LIMIT 12;

SELECT COUNT(*) AS hours, MIN(hr_utc) AS first, MAX(hr_utc) AS last,
       COUNT(nwm_cfs) AS nwm_hours, COUNT(nws_oliver_cfs) AS nws_hours, COUNT(rain_mm) AS rain_hours
FROM water.hourly_compare;
