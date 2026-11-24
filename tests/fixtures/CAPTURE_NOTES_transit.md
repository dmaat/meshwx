# Transit fixture capture notes (meshwx)

The MBTA fixtures were captured live on **2026-11-13** (a Friday; 07:38-07:44 EST in Boston = AM peak) from public
endpoints called without any API key. `oba_arrivals_sample.json` is synthetic (section 5).
No personal data is contained. A generic User-Agent was sent. All epochs are **seconds since 1970-01-01 UTC** unless marked ms.

**Data notice.** `gtfs_rt_tripupdates_sample.pb`, `gtfs_static_sample.zip`, `mbta_predictions_sample.json` and
`mbta_predictions_bus_1317_sample.json` contain data provided by the Massachusetts Department of Transportation
(MassDOT) / MBTA, used and redistributed under the
[MassDOT Developers License Agreement](https://cdn.mbta.com/sites/default/files/2023-08/mbta-massdot-develop-license-agreement.pdf).
MassDOT keeps all rights in this data. The files were re-serialized and, where noted below, trimmed for the tests; the
project's MIT License does not cover them. `oba_arrivals_sample.json` is made-up data written for the tests.

## Inventory

| File | Bytes | Source | Captured (UTC) | Frozen `now` for tests |
|---|---|---|---|---|
| `gtfs_rt_tripupdates_sample.pb` | 10614 | `https://cdn.mbta.com/realtime/TripUpdates.pb` (20 of 1694 entities kept) | 2026-11-13T12:38:45Z (HTTP Date) | `header.timestamp` = **1794573523** (2026-11-13T12:38:43Z) |
| `gtfs_static_sample.zip` | 5266 | `https://cdn.mbta.com/MBTA_GTFS.zip`, feed_version `Fall 2026, 2026-11-10T20:23:51+00:00, version D` (Last-Modified Wed, 11 Nov 2026 02:20:25 GMT) | ~2026-11-13T12:40Z (CloudFront `Date` 12:22:34Z + `Age` 1051 s) | n/a |
| `mbta_predictions_sample.json` | 16864 | `https://api-v3.mbta.com/predictions?filter%5Bstop%5D=place-pktrm&include=route%2Ctrip&sort=arrival_time&page%5Blimit%5D=10` (first 8 of 10 kept) | 2026-11-13T12:43:54Z | **1794573834** (from HTTP `Date`) |
| `mbta_predictions_bus_1317_sample.json` (extra) | 17374 | `https://api-v3.mbta.com/predictions?filter%5Bstop%5D=1317&include=route%2Ctrip&sort=arrival_time&page%5Blimit%5D=8` | 2026-11-13T12:43:59Z | **1794573839** (from HTTP `Date`) |
| `oba_arrivals_sample.json` | 10024 | Synthetic: made-up agency, routes, stop and times in the format of the OneBusAway `arrivals-and-departures-for-stop` response (section 5) | n/a | `currentTime` = **1794571200250 ms** |

The captured JSON fixtures were re-serialized with `indent=2` (key order preserved); data values are untouched. The `.pb` is a
re-serialized `FeedMessage` (header copied verbatim, entities copied whole and in original feed order). CSV rows in the
zip are byte-for-byte copies of the original lines (LF line endings, **no BOM** in MBTA's files, quoted fields kept).

## Reference rules used to compute every expected value below

Shared countdown rule (MBTA Real-Time Display Guidelines): `secs = t - now`; label = `"Now"` if `secs <= 30`,
else `f"{(secs + 30) // 60}m"` (round half up). `min_floor = secs // 60` is listed too in case tests prefer floor.
Sort arrivals by `(t, trip_id)` (two trips in the GTFS-RT sample share t = 1794574704).

**GTFS-RT** (per entity -> trip_update -> stop_time_update whose `stop_id` is a target):
1. drop if `trip.schedule_relationship` in {CANCELED=3, DELETED=7};
2. drop if `stu.schedule_relationship` in {SKIPPED=1, NO_DATA=2};
3. `arr = arrival.time` if `HasField('arrival') and arrival.HasField('time') and time > 0` else None; same for `dep`; drop if both None;
4. drop if `dep is None` and the STU is the trip's **last** STU and some STU of the trip has a departure (drop-off only);
5. drop if `(dep if dep is not None else arr) < now` (already left);
6. `t = arr if arr is not None else dep`;
7. headsign = static `trips.txt` `trip_headsign` by `trip_id` -> RT `trip_update.trip_properties.trip_headsign` -> `stop_name` of the trip's last STU stop -> `f"dir{direction_id}"`.

**MBTA V3**: drop if `schedule_relationship` in {`CANCELLED`, `SKIPPED`, `NO_DATA`}; drop if `departure_time` is null; drop if departure < now;
`t = arrival_time or departure_time` (`datetime.fromisoformat(...)` -> epoch); headsign = `attributes.trip_headsign` -> included trip `headsign` -> route `direction_destinations[direction_id]`.

**OneBusAway**: `now = currentTime` (ms); `t_ms = predictedArrivalTime` if `predicted` and `predictedArrivalTime > 0` else `scheduledArrivalTime`;
`secs = (t_ms - now_ms) // 1000`; suggested display appends `*` when the time is schedule-only.

## 1. `gtfs_rt_tripupdates_sample.pb` (MBTA GTFS-Realtime TripUpdates)

- header: `gtfs_realtime_version` = "2.0", `incrementality` = FULL_DATASET, `timestamp` = 1794573523 (2026-11-13T12:38:43Z); `feed_version` absent.
- 20 entities, all `trip_update` (no vehicle/alert); `entity.id == trip.trip_id` for every entity. Total stop_time_updates: 339.
- Entity order: `78611569, 77885757, 77886087, 77886088, 78611546, 78174862, 78612401, 78612398-OL1, 78174864, 77885786, 78174859, 77885744, 78174863, 77885763, 77885736, 78612208, 77885973, 77885974, 77885728, 77885769`.
- `trip.schedule_relationship` counts: {'SCHEDULED': 17, 'CANCELED': 2, 'ADDED': 1} (SCHEDULED is mostly the *default*, i.e. field unset).
- CANCELED trips: `77886087` (Red, dir 0, 18 STUs, all SKIPPED, no times, no vehicle) and `78174859` (route 66, dir 1, a single SKIPPED STU at 1317, empty `start_time`).
- ADDED trip: `78612398-OL1` (route 39, dir 1) - **not in trips.txt**; its STU at 1317 is departure-only (the trip starts there).
- Every non-skipped STU carries absolute `arrival.time` and/or `departure.time` (no delay-only STUs were seen anywhere in the full capture). `uncertainty` present on Red Line STUs (60 = trip in progress, 120 = waiting at terminal, 360 = train still on previous trip); absent on bus STUs at 1317.
- Vehicle: Red `vehicle.id` like `R-548CA869` with `label` = car number string (e.g. `1756`; may be empty); buses `y1258` / `1258`. Canceled trips have no `vehicle` and no `trip_update.timestamp`.
- Chosen stops: **70075** = Park Street, Red Line platform_name "Ashmont/Braintree" (dir 0); **70076** = Park Street, "Alewife" (dir 1); both children of parent **place-pktrm**. **1317** = "Huntington Ave opp Fenwood Rd" (routes 39 and 66, both dir 1 / inbound).
- Route ids: `Red` (route_type 1, `route_short_name` EMPTY, long name "Red Line"), `39`, `66` (route_type 3).

### Expected: stop 70075 (Park Street Red, toward Ashmont/Braintree) (now = 1794573523)

| # | trip_id | route | dir | headsign (source) | arrival.time | departure.time | t | secs | label | floor | vehicle.label |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 77885786 | Red | 0 | Ashmont (trips.txt) | 1794573631 | 1794573715 | 1794573631 | 108 | 2m | 1 | 1756 |
| 2 | 77885728 | Red | 0 | Ashmont (trips.txt) | 1794573862 | 1794573946 | 1794573862 | 339 | 6m | 5 | 1821 |
| 3 | 77886088 | Red | 0 | Braintree (trips.txt) | 1794574236 | 1794574320 | 1794574236 | 713 | 12m | 11 | 1747 |
| 4 | 77885736 | Red | 0 | Ashmont (trips.txt) | 1794574445 | 1794574529 | 1794574445 | 922 | 15m | 15 | 1952 |
| 5 | 77885744 | Red | 0 | Ashmont (trips.txt) | 1794574704 | 1794574788 | 1794574704 | 1181 | 20m | 19 |  |

Dropped (must NOT be displayed): `77886087` @ 70075 (Red, trip CANCELED, stu SKIPPED) -> trip CANCELED/DELETED

### Expected: stop 70076 (Park Street Red, toward Alewife) (now = 1794573523)

| # | trip_id | route | dir | headsign (source) | arrival.time | departure.time | t | secs | label | floor | vehicle.label |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 77885973 | Red | 1 | Alewife (trips.txt) | 1794573827 | 1794573891 | 1794573827 | 304 | 5m | 5 | 1849 |
| 2 | 77885757 | Red | 1 | Alewife (trips.txt) | 1794573997 | 1794574061 | 1794573997 | 474 | 8m | 7 | 1946 |
| 3 | 77885974 | Red | 1 | Alewife (trips.txt) | 1794574161 | 1794574225 | 1794574161 | 638 | 11m | 10 | 1851 |
| 4 | 77885763 | Red | 1 | Alewife (trips.txt) | 1794574302 | 1794574366 | 1794574302 | 779 | 13m | 12 | 1912 |
| 5 | 77885769 | Red | 1 | Alewife (trips.txt) | 1794574704 | 1794574768 | 1794574704 | 1181 | 20m | 19 | 1708 |

### Expected: parent station place-pktrm = {70075, 70076} merged (now = 1794573523)

| # | trip_id | route | dir | headsign (source) | arrival.time | departure.time | t | secs | label | floor | vehicle.label |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 77885786 | Red | 0 | Ashmont (trips.txt) | 1794573631 | 1794573715 | 1794573631 | 108 | 2m | 1 | 1756 |
| 2 | 77885973 | Red | 1 | Alewife (trips.txt) | 1794573827 | 1794573891 | 1794573827 | 304 | 5m | 5 | 1849 |
| 3 | 77885728 | Red | 0 | Ashmont (trips.txt) | 1794573862 | 1794573946 | 1794573862 | 339 | 6m | 5 | 1821 |
| 4 | 77885757 | Red | 1 | Alewife (trips.txt) | 1794573997 | 1794574061 | 1794573997 | 474 | 8m | 7 | 1946 |
| 5 | 77885974 | Red | 1 | Alewife (trips.txt) | 1794574161 | 1794574225 | 1794574161 | 638 | 11m | 10 | 1851 |
| 6 | 77886088 | Red | 0 | Braintree (trips.txt) | 1794574236 | 1794574320 | 1794574236 | 713 | 12m | 11 | 1747 |
| 7 | 77885763 | Red | 1 | Alewife (trips.txt) | 1794574302 | 1794574366 | 1794574302 | 779 | 13m | 12 | 1912 |
| 8 | 77885736 | Red | 0 | Ashmont (trips.txt) | 1794574445 | 1794574529 | 1794574445 | 922 | 15m | 15 | 1952 |
| 9 | 77885744 | Red | 0 | Ashmont (trips.txt) | 1794574704 | 1794574788 | 1794574704 | 1181 | 20m | 19 |  |
| 10 | 77885769 | Red | 1 | Alewife (trips.txt) | 1794574704 | 1794574768 | 1794574704 | 1181 | 20m | 19 | 1708 |

Dropped (must NOT be displayed): `77886087` @ 70075 (Red, trip CANCELED, stu SKIPPED) -> trip CANCELED/DELETED

### Expected: stop 1317 (Huntington Ave opp Fenwood Rd) (now = 1794573523)

| # | trip_id | route | dir | headsign (source) | arrival.time | departure.time | t | secs | label | floor | vehicle.label |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 78611546 | 39 | 1 | Back Bay (trips.txt) | 1794573858 | 1794573858 | 1794573858 | 335 | 6m | 5 | 1258 |
| 2 | 78612398-OL1 | 39 | 1 | Back Bay (last_stu_stop_name) | None | 1794574095 | 1794574095 | 572 | 10m | 9 | 3263 |
| 3 | 78174862 | 66 | 1 | Nubian via Allston (trips.txt) | 1794574170 | 1794574170 | 1794574170 | 647 | 11m | 10 | 3295 |
| 4 | 78611569 | 39 | 1 | Back Bay (trips.txt) | 1794574656 | 1794574656 | 1794574656 | 1133 | 19m | 18 | 1269 |
| 5 | 78612208 | 39 | 1 | Back Bay (trips.txt) | 1794574855 | 1794574855 | 1794574855 | 1332 | 22m | 22 | 1320 |
| 6 | 78174863 | 66 | 1 | Nubian via Allston (trips.txt) | 1794574896 | 1794574896 | 1794574896 | 1373 | 23m | 22 | 3296 |
| 7 | 78612401 | 39 | 1 | Back Bay (trips.txt) | 1794575187 | 1794575187 | 1794575187 | 1664 | 28m | 27 |  |
| 8 | 78174864 | 66 | 1 | Nubian via Allston (trips.txt) | 1794575322 | 1794575322 | 1794575322 | 1799 | 30m | 29 | 3234 |

Dropped (must NOT be displayed): `78174859` @ 1317 (66, trip CANCELED, stu SKIPPED) -> trip CANCELED/DELETED

Suggested rendered lines (groups ordered by first arrival, max 3 times per headsign):

- `Red@Park St: Ashmont 2m,6m,15m | Alewife 5m,8m,11m | Braintree 12m`
- `39/66@Fenwood: 39 Back Bay 6m,10m,19m | 66 Nubian via Allston 11m,23m,30m`

Byte-prefilter check: the needle for stop `S` is `b"\x22" + bytes([len(S)]) + S.encode()` (field 4, wire type 2).
For targets {70075, 70076, 1317} all 20 entities match; for {70075} alone 6 match (5 shown + canceled 77886087); for {1317} alone 9 match (8 shown + canceled 78174859).

## 2. `gtfs_static_sample.zip` (subset of MBTA static GTFS)

- Members: `routes.txt` (3 rows), `stops.txt` (119 rows), `trips.txt` (19 rows); header rows are the real MBTA headers; BOM: {'routes': False, 'stops': False, 'trips': False}; LF line endings.
- Rows kept: every route_id / trip_id / stop_id referenced by the `.pb` sample, **plus the parent stations** (`location_type=1`) of those stops so parent->child expansion can be tested.
- `stops.txt` has 25 rows with quoted fields containing commas (parent-station `stop_address`, e.g. `"Tremont St and Winter St, Boston, MA 02108"`) - a naive `split(',')` breaks; use `csv`.
- Header `routes.txt`: `route_id,agency_id,route_short_name,route_long_name,route_desc,route_type,route_url,route_color,route_text_color,route_sort_order,route_fare_class,line_id,listed_route,network_id`
- Header `stops.txt`: `stop_id,stop_code,stop_name,stop_desc,platform_code,platform_name,stop_lat,stop_lon,zone_id,stop_address,stop_url,level_id,location_type,parent_station,wheelchair_boarding,municipality,on_street,at_street,vehicle_type`
- Header `trips.txt`: `route_id,service_id,trip_id,trip_headsign,trip_short_name,direction_id,block_id,shape_id,wheelchair_accessible,trip_route_type,route_pattern_id,bikes_allowed`
- routes: {"Red": {"short": "", "long": "Red Line", "type": "1"}, "39": {"short": "39", "long": "Forest Hills Station - Back Bay Station", "type": "3"}, "66": {"short": "66", "long": "Harvard Square - Nubian Station", "type": "3"}}
- Parent stations included (25): place-alfcl, place-andrw, place-asmnl, place-bbsta, place-brdwy, place-brntn, place-chmnl, place-cntsq, place-davis, place-dwnxg, place-fldcr, place-forhl, place-harsq, place-jfk, place-knncl, place-nqncy, place-nubn, place-pktrm, place-portr, place-qamnl, place-qnctr, place-shmnl, place-smmnl, place-sstat, place-wlsta
- Children of `place-pktrm` with location_type 0 **in this sample**: ['70075', '70076'] (the real feed also has Green Line platforms 70196-70200 and 71199 plus entrances/nodes with location_type 2/3 - filter on location_type in ('0','')).
- trip_ids referenced by the `.pb` but absent from trips.txt: ['78612398-OL1'] (ADDED trip -> headsign falls back to last-stop name "Back Bay").
- Headsigns: Red dir 0 -> "Ashmont" or "Braintree"; Red dir 1 -> "Alewife"; 39 dir 1 -> "Back Bay"; 66 dir 1 -> "Nubian via Allston".
- Last-stop names used by the fallback: 70093 "Ashmont" (platform_name "Exit Only"), 70105 "Braintree", 70061 "Alewife", 23391 "Back Bay" ("Busway"), 64000 "Nubian".
- Target stop rows: `70075` -> name "Park Street", parent "place-pktrm", platform_name "Ashmont/Braintree", location_type 0; `70076` -> name "Park Street", parent "place-pktrm", platform_name "Alewife", location_type 0; `1317` -> name "Huntington Ave opp Fenwood Rd", parent "", platform_name "", location_type 0; `place-pktrm` -> name "Park Street", parent "", platform_name "", location_type 1

## 3. `mbta_predictions_sample.json` (MBTA V3 `/predictions`, Park Street)

- `mbta_predictions_sample.json`: 8 predictions; top-level keys `data`, `included`, `jsonapi`, `links` (`links` are copied unchanged from the original paginated response).
- Frozen now = 1794573834 (HTTP Date header Fri, 13 Nov 2026 12:43:54 GMT). Times are ISO 8601 with offset `-05:00` (EST).

| # (data order) | prediction id | route | trip | stop | dir | headsign | arrival_time | departure_time | sched_rel | show | t | secs | label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | prediction-77886087-70075-70-Red | Red | 77886087 | 70075 | 0 | Braintree | None | None | CANCELLED | False |  |  | CANCELLED |
| 1 | prediction-77885728-70075-70-Red | Red | 77885728 | 70075 | 0 | Ashmont | 2026-11-13T07:44:18-05:00 | 2026-11-13T07:45:42-05:00 | None | True | 1794573858 | 24 | Now |
| 2 | prediction-77885973-70076-150-Red | Red | 77885973 | 70076 | 1 | Alewife | 2026-11-13T07:44:51-05:00 | 2026-11-13T07:45:55-05:00 | None | True | 1794573891 | 57 | 1m |
| 3 | prediction-77885757-70076-150-Red | Red | 77885757 | 70076 | 1 | Alewife | 2026-11-13T07:46:04-05:00 | 2026-11-13T07:47:08-05:00 | None | True | 1794573964 | 130 | 2m |
| 4 | prediction-77885974-70076-150-Red | Red | 77885974 | 70076 | 1 | Alewife | 2026-11-13T07:49:16-05:00 | 2026-11-13T07:50:20-05:00 | None | True | 1794574156 | 322 | 5m |
| 5 | prediction-77886088-70075-70-Red | Red | 77886088 | 70075 | 0 | Braintree | 2026-11-13T07:51:04-05:00 | 2026-11-13T07:52:28-05:00 | None | True | 1794574264 | 430 | 7m |
| 6 | prediction-77885763-70076-150-Red | Red | 77885763 | 70076 | 1 | Alewife | 2026-11-13T07:51:40-05:00 | 2026-11-13T07:52:44-05:00 | None | True | 1794574300 | 466 | 8m |
| 7 | prediction-77885736-70075-70-Red | Red | 77885736 | 70075 | 0 | Ashmont | 2026-11-13T07:56:45-05:00 | 2026-11-13T07:58:09-05:00 | None | True | 1794574605 | 771 | 13m |

Shown, sorted by t: Red Ashmont 24s=Now, Red Alewife 57s=1m, Red Alewife 130s=2m, Red Alewife 322s=5m, Red Braintree 430s=7m, Red Alewife 466s=8m, Red Ashmont 771s=13m

- `included`: route `Red` (`short_name` "", `long_name` "Red Line", `type` 1, `direction_names` ["South","North"], `direction_destinations` ["Ashmont/Braintree","Alewife"]) and 8 trips (`attributes.headsign`).
- Note the API's `sort=arrival_time` puts the CANCELLED prediction (null times) **first**; null-time rows consume `page[limit]` slots.
- Every `attributes.trip_headsign` is null here (it is only set when the headsign differs from the trip's).
- Suggested rendered line: `Red@Park St: Ashmont Now,13m | Alewife 1m,2m,5m | Braintree 7m`

## 4. `mbta_predictions_bus_1317_sample.json` (extra: bus edge cases)

- `mbta_predictions_bus_1317_sample.json`: 8 predictions; top-level keys `data`, `included`, `jsonapi`, `links` (`links` are copied unchanged from the original paginated response).
- Frozen now = 1794573839 (HTTP Date header Fri, 13 Nov 2026 12:43:59 GMT). Times are ISO 8601 with offset `-05:00` (EST).

| # (data order) | prediction id | route | trip | stop | dir | headsign | arrival_time | departure_time | sched_rel | show | t | secs | label |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 0 | prediction-78612398-OL1-1317-15-39 | 39 | 78612398-OL1 | 1317 | 1 | Back Bay | None | 2026-11-13T07:49:00-05:00 | ADDED | True | 1794574140 | 301 | 5m |
| 1 | prediction-78611569-OL1-1317-15-39 | 39 | 78611569-OL1 | 1317 | 1 | Back Bay | None | 2026-11-13T07:59:00-05:00 | ADDED | True | 1794574740 | 901 | 15m |
| 2 | prediction-78174862-OL1-1317-27-66 | 66 | 78174862-OL1 | 1317 | 1 | Nubian via Allston | None | 2026-11-13T08:10:45-05:00 | ADDED | True | 1794575445 | 1606 | 27m |
| 3 | prediction-78174859-1317-27-66 | 66 | 78174859 | 1317 | 1 | Nubian via Allston | None | None | CANCELLED | False |  |  | CANCELLED |
| 4 | prediction-78611546-1317-15-39 | 39 | 78611546 | 1317 | 1 | Back Bay | 2026-11-13T07:45:26-05:00 | 2026-11-13T07:45:26-05:00 | None | True | 1794573926 | 87 | 1m |
| 5 | prediction-78174862-1317-27-66 | 66 | 78174862 | 1317 | 1 | Nubian via Allston | 2026-11-13T07:50:14-05:00 | 2026-11-13T07:50:14-05:00 | None | True | 1794574214 | 375 | 6m |
| 6 | prediction-78611569-1317-15-39 | 39 | 78611569 | 1317 | 1 | Back Bay | 2026-11-13T07:56:42-05:00 | 2026-11-13T07:56:42-05:00 | None | True | 1794574602 | 763 | 13m |
| 7 | prediction-78612208-1317-15-39 | 39 | 78612208 | 1317 | 1 | Back Bay | 2026-11-13T07:58:53-05:00 | 2026-11-13T07:58:53-05:00 | None | True | 1794574733 | 894 | 15m |

Shown, sorted by t: 39 Back Bay 87s=1m, 39 Back Bay 301s=5m, 66 Nubian via Allston 375s=6m, 39 Back Bay 763s=13m, 39 Back Bay 894s=15m, 39 Back Bay 901s=15m, 66 Nubian via Allston 1606s=27m

- Shows the sort quirk: the 3 ADDED `-OL1` trips (null `arrival_time`, trip starts at this stop) and the CANCELLED trip sort before all timed rows.
- ADDED trips are real departures: show them using `departure_time`.
- Suggested rendered line: `39 Back Bay 1m,5m,13m | 66 Nubian via Allston 6m,27m`

## 5. `oba_arrivals_sample.json` (synthetic OneBusAway document, stop 1_10001)

- Made-up data written for the tests (agency "Example Transit", routes 5/8/12, stop `1_10001` "Main St & 1st Ave",
  agency timezone America/Los_Angeles) in the format of a live OneBusAway Puget Sound response.
  `currentTime` = 1794571200250 ms (2026-11-13T12:00:00.250Z). `version` 2, `code` 200.
- It keeps the quirks of the live API: all times are epoch **milliseconds** and `currentTime` has a sub-second part;
  `predictedArrivalTime`/`predictedDepartureTime` are **0** when `predicted` is false.
- `arrivalsAndDepartures[].tripHeadsign` ("Downtown") differs from `references.trips[].tripHeadsign` ("Downtown Harbor
  Station") - use the arrival-level value. The trip direction is only in `references.trips[].directionId`.
- Predicted arrivals have a scheduled time that differs from the prediction (bus early or late), so a provider that
  used the schedule instead of the prediction fails the tests.
- Route names: `routeShortName` 5/8/12 (also in `references.routes[].shortName`); `routeLongName` is empty.

| # | routeShortName | routeId | tripId | tripHeadsign | predicted | predictedArrivalTime | scheduledArrivalTime | t_ms used | source | secs | label |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | 5 | 1_100001 | 1_900001 | Downtown | True | 1794572100000 | 1794572040000 | 1794572100000 | predicted | 899 | 15m |
| 2 | 5 | 1_100001 | 1_900002 | Downtown | False | 0 | 1794573180000 | 1794573180000 | scheduled | 1979 | 33m* |
| 3 | 8 | 1_100002 | 1_900003 | Hillcrest | True | 1794573690000 | 1794573720000 | 1794573690000 | predicted | 2489 | 41m |
| 4 | 12 | 1_100003 | 1_900004 | Riverside Park | False | 0 | 1794574050000 | 1794574050000 | scheduled | 2849 | 47m* |
| 5 | 5 | 1_100001 | 1_900005 | Downtown | True | 1794574530000 | 1794574410000 | 1794574530000 | predicted | 3329 | 55m |

- Suggested rendered line: `5 Downtown 15m,33m*,55m | 8 Hillcrest 41m | 12 Riverside Park 47m*`

## Raw-capture facts worth asserting elsewhere

- Full MBTA TripUpdates.pb at capture: 1,033,351 bytes (382,007 gzip on the wire), 1,694 entities, 35,509 STUs; 21 CANCELED, 36 ADDED trips; 762 SKIPPED STUs; 0 delay-only STUs; 103 STUs older than now-120 s (one ADDED bus trip had STUs 6.5 h in the past) -> always drop past times.
- MBTA static zip: 22,137,086 bytes, 32 members, 161.6 MB uncompressed; stop_times.txt alone 130 MB (never load on a Pi Zero); trips.txt 108,270 rows; stops.txt 10,290 rows; routes.txt 403 rows.
