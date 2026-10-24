# Data

RiverTruth does not store raw datasets in the repository. All data is fetched live from public APIs by Zetaris
(`sql/01_register_sources.sql`) and cached in Zetaris; `scripts/refresh_data.py` re-fetches it.

| Source | Endpoint (registered as a Zetaris Lightning REST table) | Subset | Licence |
|---|---|---|---|
| USGS NWIS instantaneous values | `waterservices.usgs.gov/nwis/iv/?sites=02465000,02465005&parameterCd=00060,00065&period=P7D` | 15-minute discharge and gage height, last 7 days (~2,000 values) | US public domain |
| NOAA NWPS gauge TODA1 | `api.water.noaa.gov/nwps/v1/gauges/TODA1` (+ `/stageflow/observed`, `/stageflow/forecast`) | Metadata, flood stages, ~30 days observed, ~5 days forecast | US public domain |
| NOAA National Water Model | `api.water.noaa.gov/nwps/v1/reaches/18229923/streamflow` | Analysis/assimilation streamflow, ~2 days hourly | US public domain |
| Open-Meteo | `api.open-meteo.com/v1/forecast?latitude=33.21&longitude=-87.59&hourly=precipitation&past_days=7&forecast_days=2` | Hourly precipitation, 9 days | CC BY 4.0 |

`data/sample/` holds the recorded SQL results and model replies of real runs (keyed by a hash of the query or
prompt), which is what sample mode replays. `data/world/` (git-ignored) is the live World Model store.

Limitations: USGS data are provisional; the NWM window via NWPS is about two days; Open-Meteo rainfall is modelled
at a single point, not a basin average.
