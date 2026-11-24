# Weather / geocoding fixture capture notes

All fixtures in this folder whose names start with `zippopotam_`, `openmeteo_` or `nws_` are copies
of real HTTP responses (a few trimmed, as the table notes) captured live on 2026-11-13 (UTC) for the sample public location
Boston, MA ZIP 02108 (plus a few edge-case locations listed below).

Capture method:

- Python 3.13 `requests` 2.34.2 (project venv), default redirect following, 30 s timeout.
- Header on every request: `User-Agent: meshwx-dev/0.1 (github.com/meshwx)` (a placeholder contact used only for
  these captures; it is not this project's address).
- Extra header on every api.weather.gov request: `Accept: application/geo+json`.
- NWS coordinates are the Zippopotam.us coordinates for the ZIP (already 4 decimal places):
  02108 -> 42.3576,-71.0684; 99501 -> 61.2116,-149.8761; 00901 (via /pr/) -> 18.4659,-66.1036.
- Capture time = UTC time the request was sent (second precision).
- Bodies were re-serialised as pretty JSON (2-space indent, UTF-8, `ensure_ascii=False`, LF line endings).
  Key order and values are unchanged; only number formatting may differ (e.g. Open-Meteo `0.000` -> `0.0`).

**Data licences.** These files are third-party data; the project's MIT License does not cover them.

- `nws_*.json`: National Weather Service (api.weather.gov) data, a US government work in the public domain.
- `openmeteo_*.json`: "Weather data by Open-Meteo.com" (https://open-meteo.com/), licensed
  [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/). `openmeteo_geocode_02108.json` also contains
  GeoNames data (https://www.geonames.org/, CC BY 4.0).
- `zippopotam_*.json`: Zippopotam.us (https://zippopotam.us/) data from GeoNames, under the Open Database License (ODbL).
- Changes: re-serialised as described above; trimmed responses are marked in the table below.

## Requested fixtures

| Fixture | Exact URL requested | Captured (UTC) | HTTP | Cache-Control at capture | Trimming / notes |
|---|---|---|---|---|---|
| `zippopotam_02108.json` | `https://api.zippopotam.us/us/02108` | 2026-11-13T12:38:11Z | 200 | `max-age=14400` | Full response (untrimmed). |
| `zippopotam_99501.json` | `https://api.zippopotam.us/us/99501` | 2026-11-13T12:38:12Z | 200 | `max-age=14400` | Full response (untrimmed). |
| `openmeteo_geocode_02108.json` | `https://geocoding-api.open-meteo.com/v1/search?name=02108&count=5&language=en&format=json&countryCode=US` | 2026-11-13T12:38:13Z | 200 | `public, max-age=86400` | Full response (untrimmed). |
| `nws_points_02108.json` | `https://api.weather.gov/points/42.3576,-71.0684` | 2026-11-13T12:38:16Z | 200 | `public, max-age=86400, s-maxage=86400` | Full response (untrimmed). |
| `nws_forecast_02108.json` | `https://api.weather.gov/gridpoints/BOX/71,101/forecast` | 2026-11-13T12:38:17Z | 200 | `public, max-age=250, s-maxage=3600` | Full response, all 14 periods (untrimmed). CDN-cached copy: generatedAt 11:42:27Z. |
| `nws_forecast_hourly_02108.json` | `https://api.weather.gov/gridpoints/BOX/71,101/forecast/hourly` | 2026-11-13T12:38:18Z | 200 | `public, max-age=3106, s-maxage=3600` | properties.periods trimmed to first 24 of 156; everything else untouched. |
| `nws_stations_02108.json` | `https://api.weather.gov/gridpoints/BOX/71,101/stations` | 2026-11-13T12:38:18Z | 200 | `public, max-age=51106, s-maxage=86400` | features trimmed to first 3 of 48; observationStations (48 URLs) and pagination kept intact. |
| `nws_observation_latest_02108.json` | `https://api.weather.gov/stations/KBOS/observations/latest` | 2026-11-13T12:38:18Z | 200 | `public, max-age=81, s-maxage=300` | Full response (untrimmed), first station KBOS. |
| `nws_alerts_point_02108.json` | `https://api.weather.gov/alerts/active?point=42.3576,-71.0684` | 2026-11-13T12:38:20Z | 200 | `public, max-age=5, s-maxage=5` | Full response (untrimmed); no active alerts at capture time -> features is []. |
| `nws_alerts_sample.json` | `https://api.weather.gov/alerts/active?status=actual&message_type=alert` | 2026-11-13T12:38:31Z | 200 | `public, max-age=5, s-maxage=5` | features reduced from 300 to 3 hand-picked: Flood Advisory (Polygon geometry), Freeze Warning (null geometry, onset/ends in future, expires<onset), Special Weather Statement (ends=null, instruction=null, no NWSheadline/VTEC). description trimmed to <=600 chars + '...' for: Flood Advisory. Top-level @context/type/title/updated kept. |
| `openmeteo_forecast_02108.json` | `https://api.open-meteo.com/v1/forecast?latitude=42.3576&longitude=-71.0684&current=temperature_2m,relative_humidity_2m,apparent_temperature,is_day,precipitation,weather_code,cloud_cover,pressure_msl,wind_speed_10m,wind_direction_10m,wind_gusts_10m&daily=weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max,precipitation_sum,wind_speed_10m_max,wind_gusts_10m_max,wind_direction_10m_dominant,sunrise,sunset&temperature_unit=fahrenheit&wind_speed_unit=mph&precipitation_unit=inch&timezone=auto&forecast_days=4` | 2026-11-13T12:38:34Z | 200 | (header not sent) | Full response (untrimmed). |

## Extra edge-case fixtures (not in the original request list)

| Fixture | Exact URL requested | Captured (UTC) | HTTP | Cache-Control at capture | Why it is here |
|---|---|---|---|---|---|
| `zippopotam_pr_00901.json` | `https://api.zippopotam.us/pr/00901` | 2026-11-13T12:38:13Z | 200 | `max-age=14400` | PR ZIP only resolves under /pr/ (404 under /us/); state='Pr', state abbreviation='72' (FIPS). |
| `openmeteo_geocode_00000.json` | `https://geocoding-api.open-meteo.com/v1/search?name=00000&count=5&language=en&format=json&countryCode=US` | 2026-11-13T12:38:16Z | 200 | `public, max-age=86400` | no match -> HTTP 200 with NO 'results' key. |
| `nws_points_99501.json` | `https://api.weather.gov/points/61.2116,-149.8761` | 2026-11-13T12:38:20Z | 200 | `public, max-age=86400, s-maxage=86400` | Alaska - gridId 'AER' differs from cwa 'AFC'; timeZone America/Anchorage. |
| `nws_points_marine_AS.json` | `https://api.weather.gov/points/-14.3333,-170` | 2026-11-13T12:38:33Z | 200 | `public, max-age=86307, s-maxage=86400` | HTTP 200 but type='marine' and forecast/forecastHourly/observationStations/grid* all null. |
| `nws_problem_points_404.json` | `https://api.weather.gov/points/43.6532,-79.3832` | 2026-11-13T12:38:32Z | 404 | `private, max-age=86400` | point outside NWS coverage -> HTTP 404 application/problem+json (type .../problems/InvalidPoint). |
| `nws_forecast_99501.json` | `https://api.weather.gov/gridpoints/AER/143,236/forecast` | 2026-11-13T12:38:21Z | 200 | `public, max-age=745, s-maxage=3600` | Alaska 12h forecast incl. windSpeed '0 mph' with windDirection '' and '0 to 5 mph'. |
| `nws_observation_latest_TJSJ_nulls.json` | `https://api.weather.gov/stations/TJSJ/observations/latest` | 2026-11-13T12:38:26Z | 200 | `public, max-age=260, s-maxage=300` | Puerto Rico station - every numeric value null (qualityControl 'Z') except visibility. |
| `openmeteo_error_400.json` | `https://api.open-meteo.com/v1/forecast?latitude=42.3576&longitude=-71.0684&current=temperature_2m,not_a_var` | 2026-11-13T12:38:36Z | 400 | (header not sent) | HTTP 400 error body {reason, error:true} for an unknown variable name. |
| `openmeteo_error_503.json` | `https://api.open-meteo.com/v1/forecast?latitude=42.3576&longitude=-71.0684&current=temperature_2m` (curl, same User-Agent) | 2026-11-13T12:49:01Z | 503 | (header not sent) | Transient overload seen live; body copied verbatim. Five requests between 12:48:56Z and 12:49:02Z returned 503, and a request at 12:49:02Z returned 200. Key order varied: one 503 body had `error` first. |

## Things a test author should know

- `nws_forecast_02108.json` was served from the NWS CDN cache (`generatedAt` 11:42:27Z, about 56 min before capture),
  so period 1 ("Today") starts at 06:00 local, before the capture time. Code must not assume period 1 is in the future.
- `nws_forecast_hourly_02108.json` keeps only the first 24 of 156 hourly periods. `name` and `detailedForecast` are "" in
  every hourly period, and `dewpoint` is always in `wmoUnit:degC`, even with the default `units=us`.
- `nws_stations_02108.json`: `features` were cut to 3, but `observationStations` still lists all 48 URLs, so
  `len(features) != len(observationStations)` in this fixture. Do not assert that they match.
- `nws_observation_latest_02108.json` (KBOS 12:15Z) is a 5-minute observation. `rawMessage` is "", temperature is in
  whole degrees C, and `seaLevelPressure`, `windGust` and `precipitationLast3Hours` are null. `precipitationLastHour` and
  `precipitationLast6Hours` are missing entirely, so do not use `p['precipitationLastHour']`.
- `nws_alerts_point_02108.json` has `features: []`, because no alert was active for Boston at capture time.
- `nws_alerts_sample.json` came from the filtered national feed (`status=actual&message_type=alert`: 300 features,
  about 1.2 MB; the unfiltered `/alerts/active` had 507 features, about 2.3 MB). Only 3 features were kept, and only the
  Flood Advisory description was trimmed (to <= 600 chars + `...`). The Freeze Warning has `expires` < `onset` < `ends`,
  so `expires` is not the end of the event.
- `openmeteo_*` bodies contain the UTF-8 degree sign in `*_units` (`"°F"`, `"°"`). Read them with `encoding="utf-8"`.
