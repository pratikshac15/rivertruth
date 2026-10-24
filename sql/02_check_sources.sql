-- Quick checks after 01_register_sources.sql. Run:  python3 scripts/run_sql.py sql/02_check_sources.sql
SELECT site_id, param_code, unit, COUNT(*) AS n, MIN(valid_time) AS first, MAX(valid_time) AS last FROM water.usgs_observed GROUP BY site_id, param_code, unit;
SELECT * FROM water.nwps_gauge_info;
SELECT kind, stage_unit, flow_unit, COUNT(*) AS n, MIN(valid_time) AS first, MAX(valid_time) AS last FROM water.nwps_stageflow GROUP BY kind, stage_unit, flow_unit;
SELECT series, unit, COUNT(*) AS n, MIN(valid_time) AS first, MAX(valid_time) AS last, MAX(flow) AS max_flow FROM water.nwm_streamflow GROUP BY series, unit;
SELECT unit, COUNT(*) AS n, SUM(precip) AS total, MAX(precip) AS max_hour FROM water.rain_hourly GROUP BY unit;
