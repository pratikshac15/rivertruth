-- =============================================================================
-- RiverTruth: register four public water-data APIs in Zetaris as SQL tables.
-- Demo region: Black Warrior River at Tuscaloosa/Northport, Alabama.
--
-- Sources (all public, no API key):
--   1. USGS NWIS instantaneous values: observed discharge (cfs) and gage height (ft)
--      sites 02465000 (Black Warrior R at Northport) and 02465005 (Oliver L&D tailwater)
--   2. NOAA NWPS gauge TODA1 (Oliver Lock & Dam): NWS observed + official forecast (ft, kcfs)
--   3. NOAA National Water Model via NWPS, reach 18229923: analysis + short-range flow (ft3/s)
--   4. Open-Meteo hourly precipitation at the gauge (mm), past 7 days + 2-day forecast
--
-- Run with:  python3 scripts/run_sql.py sql/01_register_sources.sql
-- (one statement at a time, "already exists" errors are skipped so re-runs are safe)
--
-- Note: a Lightning REST table re-fetches its URL on every query, so each raw table
-- is cached straight after it is created (CACHE TABLE). Re-run to refresh the data.
-- =============================================================================

-- ---------- Databases (one per source system = the "fragmented" sources) ----------
CREATE LIGHTNING DATABASE USGS_NWIS DESCRIBE BY "USGS NWIS observed instantaneous values";
CREATE LIGHTNING DATABASE NOAA_NWPS DESCRIBE BY "NOAA NWPS gauges, NWS observed and forecast";
CREATE LIGHTNING DATABASE NOAA_NWM DESCRIBE BY "NOAA National Water Model reach streamflow via NWPS";
CREATE LIGHTNING DATABASE OPEN_METEO DESCRIBE BY "Open-Meteo hourly precipitation";

CREATE SCHEMASTORE CONTAINER water;

-- ---------- 1. USGS observed ----------
CREATE LIGHTNING REST TABLE iv_black_warrior_raw FROM USGS_NWIS REQUEST(
    endpoint "https://waterservices.usgs.gov/nwis/iv/?format=json&sites=02465000,02465005&parameterCd=00060,00065&period=P7D",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE usgs_nwis.iv_black_warrior_raw;

CREATE SCHEMASTORE VIEW usgs_observed WITH CONTAINER water AS
SELECT
    ts.`sourceInfo`.`siteName`              AS site_name,
    ts.`sourceInfo`.`siteCode`[0].`value`   AS site_id,
    ts.`variable`.`variableCode`[0].`value` AS param_code,
    ts.`variable`.`unit`.`unitCode`         AS unit,
    p.`dateTime`                            AS valid_time,
    CAST(p.`value` AS DOUBLE)               AS value,
    p.`qualifiers`[0]                       AS qualifier
FROM usgs_nwis.iv_black_warrior_raw
LATERAL VIEW explode(`value`.`timeSeries`) AS ts
LATERAL VIEW explode(ts.`values`) AS v
LATERAL VIEW explode(v.`value`) AS p;

-- ---------- 2. NOAA NWPS (NWS river forecast centre) ----------
CREATE LIGHTNING REST TABLE toda1_gauge_raw FROM NOAA_NWPS REQUEST(
    endpoint "https://api.water.noaa.gov/nwps/v1/gauges/TODA1",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE noaa_nwps.toda1_gauge_raw;

CREATE LIGHTNING REST TABLE toda1_observed_raw FROM NOAA_NWPS REQUEST(
    endpoint "https://api.water.noaa.gov/nwps/v1/gauges/TODA1/stageflow/observed",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE noaa_nwps.toda1_observed_raw;

CREATE LIGHTNING REST TABLE toda1_forecast_raw FROM NOAA_NWPS REQUEST(
    endpoint "https://api.water.noaa.gov/nwps/v1/gauges/TODA1/stageflow/forecast",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE noaa_nwps.toda1_forecast_raw;

CREATE SCHEMASTORE VIEW nwps_gauge_info WITH CONTAINER water AS
SELECT
    lid, `usgsId` AS usgs_id, `reachId` AS reach_id, name, latitude, longitude,
    flood.categories.action.stage   AS action_stage_ft,
    flood.categories.minor.stage    AS minor_stage_ft,
    flood.categories.moderate.stage AS moderate_stage_ft,
    flood.categories.major.stage    AS major_stage_ft
FROM noaa_nwps.toda1_gauge_raw;

CREATE SCHEMASTORE VIEW nwps_stageflow WITH CONTAINER water AS
SELECT 'TODA1' AS lid, 'observed' AS kind, `primaryName` AS stage_name, `primaryUnits` AS stage_unit,
       `secondaryUnits` AS flow_unit, `issuedTime` AS issued_time,
       d.`validTime` AS valid_time, CAST(d.`primary` AS DOUBLE) AS stage, CAST(d.`secondary` AS DOUBLE) AS flow
FROM noaa_nwps.toda1_observed_raw LATERAL VIEW explode(data) AS d
UNION ALL
SELECT 'TODA1' AS lid, 'forecast' AS kind, `primaryName`, `primaryUnits`,
       `secondaryUnits`, `issuedTime`,
       d.`validTime`, CAST(d.`primary` AS DOUBLE), CAST(d.`secondary` AS DOUBLE)
FROM noaa_nwps.toda1_forecast_raw LATERAL VIEW explode(data) AS d;

-- ---------- 3. NOAA National Water Model ----------
CREATE LIGHTNING REST TABLE reach_18229923_analysis_raw FROM NOAA_NWM REQUEST(
    endpoint "https://api.water.noaa.gov/nwps/v1/reaches/18229923/streamflow?series=analysis_assimilation",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE noaa_nwm.reach_18229923_analysis_raw;

CREATE LIGHTNING REST TABLE reach_18229923_short_raw FROM NOAA_NWM REQUEST(
    endpoint "https://api.water.noaa.gov/nwps/v1/reaches/18229923/streamflow?series=short_range",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE noaa_nwm.reach_18229923_short_raw;

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
FROM noaa_nwm.reach_18229923_short_raw
LATERAL VIEW explode(`shortRange`.series.data) AS d;

-- ---------- 4. Rainfall ----------
CREATE LIGHTNING REST TABLE tuscaloosa_rain_raw FROM OPEN_METEO REQUEST(
    endpoint "https://api.open-meteo.com/v1/forecast?latitude=33.21&longitude=-87.59&hourly=precipitation&past_days=7&forecast_days=2&timezone=UTC",
    method "get",
    response_type "json",
    http_encoding "URLENCODED"
) HEADER (
    user-agent "rivertruth-hackathon-demo"
) BODY ();

CACHE TABLE open_meteo.tuscaloosa_rain_raw;

CREATE SCHEMASTORE VIEW rain_hourly WITH CONTAINER water AS
SELECT latitude, longitude, `hourly_units`.precipitation AS unit,
       t AS valid_time, CAST(hourly.precipitation[i] AS DOUBLE) AS precip
FROM open_meteo.tuscaloosa_rain_raw
LATERAL VIEW posexplode(hourly.time) AS i, t;
