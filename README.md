# meshwx

Weather, severe-weather alerts and real-time transit arrivals for a
[Meshtastic](https://meshtastic.org) LoRa mesh. Built to run unattended on a
Raspberry Pi Zero with a Meshtastic node plugged into its USB port.

```text
Boston, MA 2:15p: 54°F feels 50° Cloudy. Wind NW 12 G20mph. RH 65%. 30.12inHg
Boston, MA fcst:
Today: Hi 45° Chc Lt Rain then Mstly Cloudy 49%
Tonight: Lo 35° Mstly Cloudy
⚠ Tornado Warning for Boston, MA til 3:45p: Tornado Warning for... Central Suffolk County ... TAKE COVER NOW!
Park St: Red>Ashmont now,13m | Red>Alewife 1,2,5m | Red>Braintree 7m
```

*What mesh users see (examples). Every packet is at most 200 bytes.*

**Contents:**
[What it does](#what-it-does) ·
[Hardware](#hardware) ·
[Quick start](#quick-start-raspberry-pi) ·
[Configuration](#configuration) ·
[Mesh commands](#mesh-commands) ·
[Transit](#transit-arrivals) ·
[Testing without a radio](#testing-without-a-radio) ·
[CLI reference](#cli-reference) ·
[Troubleshooting](#troubleshooting) ·
[Updating and uninstalling](#updating-and-uninstalling) ·
[Data sources and terms](#data-sources-and-terms) ·
[How it works](#how-it-works)

## What it does

- **Scheduled weather broadcasts** for one US ZIP code: current conditions,
  forecast and active alerts (optionally transit arrivals) every N minutes
  (15 minutes to 1 week), at fixed times of day, or never - with optional
  quiet hours.
- **Severe-weather alerts as they happen.** Every 5 minutes meshwx asks the
  National Weather Service for alerts at your location and broadcasts each
  *new* one immediately: ahead of anything else waiting to be sent, once per
  alert (remembered across restarts), even during quiet hours.
- **Answers mesh commands** such as `!wx`, `!fc 90210`, `!alerts` or `!bus`,
  on a channel or in a direct message.
- **Real-time transit arrivals** for the stops you choose, from any
  GTFS-Realtime feed, the MBTA API, OneBusAway servers, Transitland, or SIRI
  (511.org Bay Area, MTA Bus Time New York).
- **ZIP code and broadcast interval can be changed while it runs**, from the
  mesh (admins only) or on the Pi.
- **Easy on the mesh and on a Pi Zero:** packets of at most 200 bytes
  (numbered "1/3" when a message needs several), a pause between packets
  (8 s by default), a per-node rate limit for commands, one small Python
  process, few SD-card writes, automatic radio reconnects, and a fallback to
  Open-Meteo and to cached data when a weather service is down.

## Hardware

| Part | Notes |
|---|---|
| Computer | Raspberry Pi **Zero W** or **Zero 2 W**. Any other Raspberry Pi, or a Debian/Ubuntu computer with systemd, works the same way. The original Pi Zero has no Wi-Fi, and its only USB port is needed for the node. |
| Storage | microSD card, 8 GB or more. |
| Radio | Any Meshtastic node with USB: Heltec, LilyGO (T-Beam, T-Echo, T-Deck ...), RAK WisBlock (RAK4631), Seeed, RP2040 boards ... A node on Wi-Fi/Ethernet can be used over TCP instead (`meshtastic.connection: tcp`). |
| Cable | Use the Pi Zero's port labelled **USB** (the middle one), not **PWR IN** (the one at the end), with a micro-USB OTG adapter or a micro-USB-to-USB-C cable. It must be a *data* cable: charge-only cables are common, and with one no device shows up at all. |

**Power.** The node is powered through the Pi, so the Pi's power supply feeds
both. Raspberry Pi recommends 1.2 A for a Zero / Zero W and 2 A for a Zero 2 W;
the USB port itself has no current limit beyond the supply, board and
connectors. Use a good 5 V supply of 2.5 A (for example the official 12.5 W
micro-USB supply) and a short cable, especially when the node charges a
battery. The Zero models have no under-voltage detection, so a weak supply
does not produce a warning - it shows up as the node disconnecting and
reconnecting in the log, random reboots or a corrupted SD card.

**Device names.** Linux names the node's serial port after its USB chip:

| Boards (examples) | USB chip (vendor:product) | Linux device |
|---|---|---|
| Heltec V1/V2/V3, LilyGO T-Lora v1.3, DIY ESP32 | Silicon Labs CP210x (10c4:ea60) | `/dev/ttyUSB0` |
| RAK11200 | WCH CH340 (1a86:7523) | `/dev/ttyUSB0` |
| LilyGO T-Beam, T-Lora v2.1, Nano G1 | WCH CH9102 (1a86:55d4) | `/dev/ttyACM0` |
| RAK4631, LilyGO T-Echo, other nRF52840 boards | native USB (239a:xxxx) | `/dev/ttyACM0` |
| LilyGO T-Deck, Seeed XIAO ESP32-S3, other ESP32-S3 boards | native USB (303a:1001, 2886:xxxx) | `/dev/ttyACM0` |
| RP2040 / RP2350 boards | native USB (2e8a:xxxx) | `/dev/ttyACM0` |

The installer's udev rule also gives all of these the name `/dev/meshtastic`.
You normally don't need any of these names: with `meshtastic.serial_port: ""`
meshwx finds the node by itself.

## Quick start (Raspberry Pi)

1. **Flash the SD card** with [Raspberry Pi Imager](https://www.raspberrypi.com/software/).
   - Operating system: *Raspberry Pi OS Lite (32-bit)* for a Zero or Zero W
     (they cannot run 64-bit); *Raspberry Pi OS Lite (64-bit)* for a
     Zero 2 W (32-bit works too). The current release, "Trixie" (Debian 13,
     Python 3.13), and the previous one, "Bookworm" (Python 3.11), both work.
   - In the customisation settings set a **hostname** (e.g. `meshwx`), a
     **username and password**, **Wi-Fi** (network, password, country), the
     **time zone**, and enable **SSH**.
2. **Connect the node** to the Pi's USB data port, power up the Pi (the first
   boot takes a few minutes) and log in: `ssh <user>@meshwx.local`
3. **Put meshwx on the Pi.** Either clone the repository:

   ```sh
   sudo apt install -y git
   git clone https://github.com/dmaat/meshwx.git meshwx
   ```

   or copy the folder over from your computer: `scp -r <folder> <user>@meshwx.local:meshwx`
4. **Install:**

   ```sh
   cd meshwx
   sudo bash deploy/install.sh
   ```

   A setup wizard asks for the essentials (such as your ZIP code) and writes
   `/etc/meshwx/config.yaml`; then the service starts. On a Pi Zero the
   installation takes 10-20 minutes. Nothing is compiled: all packages come
   as prebuilt wheels (on 32-bit Raspberry Pi OS from [piwheels.org](https://www.piwheels.org)).
5. **Check it:** `journalctl -u meshwx -f` follows the log. From your phone,
   send the Pi's node a direct message `help`, or write `!wx` on channel 0.
   Then read [Channels and mesh etiquette](#channels-and-mesh-etiquette)
   before you leave it running.

### What the installer does

- installs `python3-venv` and `python3-pip` when missing (no compiler or other build tools);
- creates the system user `meshwx`, a member of the `dialout` group that owns serial ports;
- copies the code to `/opt/meshwx/src` and installs it with its dependencies into the
  virtualenv `/opt/meshwx/venv` (Raspberry Pi OS refuses `pip install` outside a virtualenv;
  on 32-bit ARM, prebuilt packages come from piwheels.org);
- creates `/etc/meshwx/config.yaml` with the setup wizard - or, when there is no terminal,
  from the commented example - readable only by root and `meshwx` because it may hold API
  keys. An existing config is never overwritten;
- installs the udev rule `/etc/udev/rules.d/99-meshtastic.rules`: known Meshtastic boards get
  the name `/dev/meshtastic` and group `dialout`, and ModemManager leaves them alone;
- installs and enables the systemd service `meshwx` (starts at boot, restarts after a crash);
- installs the `meshwx` command (see below).

Running it again is safe. It enables the service but does not start it while the config is
invalid, or while the config is still the untouched example and meshwx was not already running
(the example would broadcast Boston's weather on your primary channel): edit the config, then
`sudo systemctl start meshwx`. A service that was running before an update is started again.

| Option | Effect |
|---|---|
| `--upgrade` | update an existing installation: new code and dependencies, same config, no wizard |
| `--no-start` | install and enable the service, but do not (re)start it |
| `--skip-apt` | do not install system packages |

| Path | Contents |
|---|---|
| `/etc/meshwx/config.yaml` | your configuration |
| `/var/lib/meshwx/state.json` | runtime state: ZIP code / interval changes, alerts already broadcast |
| `/var/cache/meshwx/` | cached downloads |
| `/opt/meshwx/src`, `/opt/meshwx/venv` | code and virtualenv |
| `/etc/systemd/system/meshwx.service` | the service (local changes: `sudo systemctl edit meshwx`) |
| `/etc/udev/rules.d/99-meshtastic.rules` | serial port rule |
| `/usr/local/bin/meshwx` | the `meshwx` command |

### The `meshwx` command on the Pi

`meshwx ...` runs the CLI as the `meshwx` user with the service's config, state and cache
directory (it asks for your password when sudo needs one). That is why `meshwx set zip 90210`
reaches the running service. Do not run `sudo /opt/meshwx/venv/bin/meshwx set ...`: as root
the CLI writes a state file of its own that the service never reads.

To use the CLI as yourself - for example for a dry run with a copy of the config - call
`/opt/meshwx/venv/bin/meshwx` directly.

## Configuration

Edit the config and restart the service:

```sh
sudo nano /etc/meshwx/config.yaml
sudo systemctl restart meshwx
journalctl -u meshwx -n 30        # check that it started cleanly
```

Every setting is explained in the file itself (the template is
[`config.example.yaml`](config.example.yaml); `meshwx example-config` prints it). Misspelled
settings are rejected with a "did you mean ..." hint. Put node ids in quotes (`"!a1b2c3d4"`:
an unquoted `!` means something else in YAML), and preferably ZIP codes, stop and route ids
(`"1_75403"`) and times (`"22:00"`) too: YAML may read an unquoted value as a number or as
true/false.

### ZIP code

```yaml
location:
  zip_code: "02108"
```

Change it while meshwx runs:

- from the mesh (admins only): `!setzip 90210`, or `setzip 90210` in a direct message;
- on the Pi: `meshwx set zip 90210`.

The change is saved in `state.json`, so it survives restarts; a running service picks up
`meshwx set` within about 30 seconds. If you later edit `zip_code` in config.yaml, your edit
wins. `meshwx status` shows the values in effect and where they come from.

Also in `location`: `latitude`/`longitude` (an exact spot instead of the ZIP code's centre)
and `name` (display name such as "Home"). Mesh users can ask for other places too:
`!wx 90210` (turn off with `weather.allow_other_zips: false`). Outside the US, set
`location.country` and use `weather.provider: openmeteo` (no alerts there).

### Broadcast schedule

```yaml
broadcast:
  enabled: true
  channel_index: 1          # see "Channels and mesh etiquette"
  interval_minutes: 180     # 15-10080 (one week), or 0 = no scheduled broadcasts
  align_to_clock: true      # 180 -> 00:00, 03:00, 06:00 ... local time
  times: []                 # e.g. ["07:00", "18:00"]: fixed times instead of the interval
  quiet_start: "22:00"      # no scheduled broadcasts from 22:00 ...
  quiet_end: "06:00"        # ... until 06:00 (alerts still go out)
  content: [current, forecast, alerts]   # add "transit" for arrivals
  on_startup: false         # also broadcast once right after the radio connects
```

- With `align_to_clock: true` and an interval that divides a day, broadcasts happen at
  multiples of the interval after midnight; otherwise every `interval_minutes` counted from
  the start of the service.
- Change the interval while meshwx runs: `!setinterval 60`, `!setinterval 2h` or
  `!setinterval off` (admins), or `meshwx set interval 60` on the Pi. `!broadcast` sends one
  right away.
- Every broadcast uses airtime that everyone in range shares. meshwx warns below 30 minutes;
  for weather, a few broadcasts a day are plenty.

### Severe-weather alerts

```yaml
broadcast:
  alert_watch: true
  alert_check_minutes: 5          # 1-240
  alert_min_severity: Moderate    # Minor | Moderate | Severe | Extreme
  alert_channel_index: null       # null = the channel of the weather broadcasts
```

- New NWS alerts for your location are broadcast as soon as they are found (within
  `alert_check_minutes`), ahead of anything already queued, and also during quiet hours.
- Each alert goes out once; meshwx remembers what it sent, also across restarts.
  Cancellations, updates, continuations and extensions of an alert already sent are not
  broadcast again (also when NWS re-issues them as a "new" alert that refers to the old
  one); an upgrade to a different event (for example a Watch becoming a Warning) is.
  Ended, cancelled and replaced NWS products are never listed or broadcast.
- An alert only counts as sent once every packet of it was handed to the radio. If the
  radio was down too long, sending failed, or the Pi restarted with the alert still queued,
  it is sent again at the next check (or about 30 s after a restart) while it is still in
  effect - after three failed tries in a row meshwx gives up on it and logs an error.
- Severity order: Minor < Moderate < Severe < Extreme; alerts without a severity count as
  Moderate.
- US only: needs `weather.provider: nws` and `broadcast.enabled: true`.
- `!alerts` lists the active alerts, `!alerts 2` shows the details of the second one.
- Routine weather on a secondary channel but alerts on the primary channel
  (`alert_channel_index: 0`) is possible - ask your local mesh community first.

> meshwx relays NWS alerts on a best-effort basis: the internet connection, the NWS service,
> the Pi, the radio or the mesh can fail. Never rely on it as your only source of warnings.

### Channels and mesh etiquette

A mesh is shared airtime: while one node transmits, nearby nodes wait. Automated messages
are welcome when they are useful and rare, and unwelcome when they crowd out people.

- **Use a secondary channel, not the public primary channel (LongFast).** Add a channel such
  as "Weather" to the Pi's node and share it with the people who want weather; everybody else
  never sees the broadcasts. Then set `broadcast.channel_index` to its index and add that
  index to `commands.channels`:

  ```yaml
  broadcast:
    channel_index: 1
  commands:
    channels: [0, 1]       # where meshwx listens for commands; DMs always work
  ```

  You can add the channel with the Meshtastic app (before you connect the node to the Pi), or
  on the Pi with the `meshtastic` command-line tool that comes with meshwx (stop meshwx first:
  only one program can use the serial port at a time):

  ```sh
  sudo systemctl stop meshwx
  sudo /opt/meshwx/venv/bin/meshtastic --port /dev/meshtastic --ch-add Weather   # random key
  sudo /opt/meshwx/venv/bin/meshtastic --port /dev/meshtastic --qr-all           # URLs to share
  sudo systemctl start meshwx
  ```

  (`/dev/meshtastic` exists for the boards in the udev rule; otherwise use the node's
  `/dev/ttyUSB0` or `/dev/ttyACM0`.) `--ch-add` prints the index the new channel got.

- **A secondary channel saves people from noise, not airtime**: all channels share the same
  radio settings. Keep broadcasts infrequent (the default is every 3 hours) and short
  (`content`), and leave `meshtastic.message_delay_seconds` at 8 or more.
- **Keep the hop limit at the node's default** (`meshtastic.hop_limit: null`, usually 3).
  More hops repeat every packet further and cost everyone more airtime.
- **MQTT:** if the node uplinks the channel to an MQTT server, every broadcast is also
  published to the internet (and to busy public servers). Leave uplink off for the weather
  channel unless you want that.
- **Ask your local mesh community** (Discord, forum, club) before broadcasting on a shared
  channel; many communities have rules for bots.

### Admins

```yaml
commands:
  admins: ["!a1b2c3d4"]
```

Admins can use `setzip`, `setinterval` and `broadcast`, and are not rate-limited. Use the id
of *your own* node (the one paired with your phone), not the Pi's node: in the Meshtastic
app, open the node list and select your node; its id looks like `!a1b2c3d4` (8 hex digits).

Admin rights rest on the sender id that comes with each message, and that id can be faked
(most easily through MQTT). meshwx accepts direct messages, the usual way to send admin
commands, on every channel, so keep MQTT downlink off on all channels of the Pi's node, or
leave `admins` empty and change settings on the Pi with `meshwx set`.

## Mesh commands

On a channel, commands need the prefix (`!` by default, `commands.prefix`) and must arrive
on a channel listed in `commands.channels`. In a direct message to the Pi's node the prefix
is optional. Commands are not case-sensitive.

| Command | Aliases | Who | Reply |
|---|---|---|---|
| `help` | `?`, `h`, `cmds` | all | command list |
| `wx [zip]` | `weather`, `w`, `now` | all | current conditions (+ number of active alerts) |
| `fc [zip]` | `forecast`, `f` | all | forecast (1-3 packets) |
| `alerts [n]` | `alert`, `warn` | all | active alerts, or the details of alert *n* |
| `bus [stop]` | `transit`, `next`, `t` | all (if transit is enabled) | arrivals at all stops, or at one stop (label prefix or number) |
| `ping` | `test` | all | `pong` (+ hops) |
| `status` | | all | ZIP code, interval, next broadcast, radio and queue state |
| `setzip <zip>` | `zip` | admins | change the home ZIP code (saved) |
| `setinterval <min\|Nh\|off>` | `interval` | admins | change the broadcast interval (saved) |
| `broadcast` | `bcast` | admins | broadcast now |

- Replies to a channel command go to the same channel, or by direct message with
  `commands.reply_on_channel: false`. Direct messages are answered by direct message.
- Each node can run one command every `commands.cooldown_seconds` (30 by default). A
  direct message sent too soon gets one "please wait" answer; a channel command sent too
  soon is ignored.
- An unknown command gets a hint in a direct message and is ignored on a channel, where
  people chat.

## Transit arrivals

```yaml
transit:
  enabled: true
  provider: mbta                # gtfs_rt | mbta | onebusaway | transitland | siri
  stops:
    - id: "place-pktrm"         # the provider's stop id
      label: "Park St"          # shown on the mesh; users type "!bus park"
      routes: ["Red"]           # optional: only these routes (route id or name)
      # direction_id: 0         # optional: only this GTFS direction (0 or 1)
  max_arrivals: 3               # per route and destination
  horizon_minutes: 90           # ignore arrivals further out
  min_minutes: 0                # hide arrivals sooner than this (walking time)
  cache_seconds: 60             # share one answer between users for this long
```

`!bus` shows all stops, `!bus park` the stop whose label starts with "park", `!bus 2` the
second stop. Add `transit` to `broadcast.content` to include arrivals in the scheduled
broadcasts. Times are minutes from now (`now`, `2m`, `1,2,5m`); a `*` marks a scheduled time
without real-time data. Each provider section below shows a complete example.

### MBTA (Boston) - `mbta`

No key needed; a free key from [api-v3.mbta.com](https://api-v3.mbta.com/register) raises
the rate limit (20 to 1000 requests per minute).

```yaml
transit:
  enabled: true
  provider: mbta
  stops:
    - id: "place-pktrm"      # Park Street station: all platforms
      label: "Park St"
      routes: ["Red"]
    - id: "1317"             # a bus stop (number printed on the sign)
      label: "Fenwood"
  mbta:
    api_key: ""
```

Stop ids: open the stop on [mbta.com](https://www.mbta.com/stops) - the id is the last part of
its address, e.g. `mbta.com/stops/place-pktrm` or `mbta.com/stops/1317`.

### Any GTFS-Realtime feed - `gtfs_rt`

Works with every agency that publishes a GTFS-Realtime *TripUpdates* feed. Also set the
agency's static GTFS zip: it provides route names and destinations, and it lets you use a
whole station (a parent stop such as `place-pktrm`) instead of single platforms. The zip
(20-50 MB for a big agency) is downloaded in the background, kept on disk and downloaded
again only when it has changed, or after `static_refresh_hours`. meshwx then indexes only
what your stops need (route names, stop names, destinations, and which trips end at your
stop, so a train that terminates there is not shown as one you can board). On a Pi Zero the
first index of a big agency takes a few minutes; until it is ready, `!bus` may answer
"Stop data loading, try later". A stop id that does not exist in the agency's `stops.txt`
answers "Stop not found" (the log names the right `stop_id` if you used a stop *code*).

MBTA, no key:

```yaml
transit:
  enabled: true
  provider: gtfs_rt
  stops:
    - id: "place-pktrm"      # parent station: expanded to its platforms
      label: "Park St"
      routes: ["Red"]
    - id: "70075"            # one platform: Red Line toward Ashmont/Braintree
      label: "Park St SB"
  gtfs_rt:
    trip_updates_url: "https://cdn.mbta.com/realtime/TripUpdates.pb"
    static_gtfs_url: "https://cdn.mbta.com/MBTA_GTFS.zip"
```

Feeds with a key in the URL put it in `params`. Chicago (CTA - request a key in the
[CTA Developer Center](https://www.transitchicago.com/developers/)):

```yaml
  gtfs_rt:
    trip_updates_url: "https://transitdata.transitchicago.com/GtfsRealtime/TripUpdates.pb"
    static_gtfs_url: "https://www.transitchicago.com/downloads/sch_data/google_transit.zip"
    params: {key: "YOUR_CTA_KEY"}
```

Portland (TriMet - register for an AppID at [developer.trimet.org](https://developer.trimet.org/)):

```yaml
  gtfs_rt:
    trip_updates_url: "https://developer.trimet.org/ws/V1/TripUpdate"
    static_gtfs_url: "https://developer.trimet.org/schedule/gtfs.zip"
    params: {appID: "YOUR_TRIMET_APPID"}
```

A key sent as an HTTP header goes in `headers` instead, e.g. `headers: {x-api-key: "KEY"}`.

Stop ids: the `stop_id` column of `stops.txt` inside the static GTFS zip, for example
`unzip -p google_transit.zip stops.txt | grep -i "clark"` (`sudo apt install unzip` if needed).
Many agencies also print the stop id on the stop sign or show it on their website.

### OneBusAway - `onebusaway`

For OneBusAway servers such as Puget Sound (Seattle area), Tampa or San Diego. Puget Sound
keys are free: email `oba_api_key@soundtransit.org` (see Sound Transit's
[Open Transit Data](https://www.soundtransit.org/help-contacts/business-information/open-transit-data-otd)
page). The shared demo key `TEST` is fine for a quick try but is often throttled.

```yaml
transit:
  enabled: true
  provider: onebusaway
  stops:
    - id: "1_75403"          # agency 1 (King County Metro) + stop 75403
      label: "Stevens Way"
  onebusaway:
    base_url: "https://api.pugetsound.onebusaway.org"
    api_key: "YOUR_KEY"
```

Stop ids are `<agency id>_<stop number>`, written in quotes like all ids; the stop number is
posted at the stop and shown in the OneBusAway app.

### Transitland - `transitland`

[Transitland](https://www.transit.land) covers thousands of agencies through one API. The free
"Explorer" plan allows 5,000 requests per month for non-commercial use - every refresh of every
stop is one request, so keep `cache_seconds` generous and transit out of frequent broadcasts.

```yaml
transit:
  enabled: true
  provider: transitland
  stops:
    - id: "f-drt-mbta:70075"   # <feed Onestop ID>:<GTFS stop_id>
      label: "Park St"
  transitland:
    api_key: "YOUR_KEY"
```

Stop ids: find the agency's feed on transit.land; its Onestop ID looks like `f-drt-mbta`.
Combine it with the GTFS `stop_id`, or use a stop's own Onestop ID (`s-...`).

### SIRI StopMonitoring - `siri`

**511.org (San Francisco Bay Area)** - free token from [511.org](https://511.org/open-data/token).
`agency` selects the operator (`SF` = Muni). The default limit is 60 requests per hour per
token, so use few stops and `cache_seconds` of 60 or more.

```yaml
transit:
  enabled: true
  provider: siri
  stops:
    - id: "15553"            # stop code posted at the stop
      label: "My stop"
  siri:
    url: "https://api.511.org/transit/StopMonitoring"
    api_key: "YOUR_511_TOKEN"
    key_param: "api_key"
    stop_param: "stopcode"
    params: {agency: "SF", format: "json"}
```

**MTA Bus Time (New York City buses)** - free key from
[register.developer.obanyc.com](https://register.developer.obanyc.com/). The stop id is the
6-digit stop code on the bus stop sign.

```yaml
transit:
  enabled: true
  provider: siri
  stops:
    - id: "308209"
      label: "My stop"
  siri:
    url: "https://bustime.mta.info/api/siri/stop-monitoring.json"
    api_key: "YOUR_MTA_KEY"
    key_param: "key"
    stop_param: "MonitoringRef"
    params: {version: "2"}
```

## Testing without a radio

`meshwx preview` fetches everything and prints the messages that would be sent, without a
radio:

```sh
meshwx preview                          # everything
meshwx preview --what current,forecast  # current, forecast, alerts, transit, broadcast or all
meshwx preview --zip 90210 --what forecast
```

`meshwx run --dry-run` runs the whole service with the terminal as the "radio": every line you
type arrives as a direct message from a fake node, and a line starting with `#N` arrives on
channel N. Everything meshwx would transmit is printed instead. Stop it with Ctrl+C.

```text
help
wx 90210
#0 !fc
bus park
```

To try admin commands, add the fake node `"!0000beef"` to `commands.admins` in the config you
test with. On the Pi, do dry runs as yourself with a copy of the config (the setup wizard
suggests the same), not with the `meshwx` command: you can then change the copy freely, and
the test never touches the service's config or state - so it cannot, for example, mark real
alerts as already sent:

```sh
sudo cat /etc/meshwx/config.yaml > ~/meshwx-test.yaml
/opt/meshwx/venv/bin/meshwx run --dry-run -c ~/meshwx-test.yaml
```

### On Windows, macOS or a Linux desktop

meshwx needs Python 3.9 or newer.

```sh
cd meshwx
python3 -m venv .venv                 # Windows: py -m venv .venv
. .venv/bin/activate                  # Windows: .venv\Scripts\activate
pip install -e ".[gtfs]"
cp config.example.yaml config.yaml    # Windows: copy config.example.yaml config.yaml
# edit config.yaml: at least location.zip_code
meshwx preview
meshwx run --dry-run
```

`config.yaml` in the current directory is found automatically (otherwise use `-c`). State
goes to `~/.local/state/meshwx` unless `state_dir` says otherwise. With a node attached,
`meshwx ports` lists the serial ports, and `meshwx run --port COM5` (Windows) or
`--port /dev/cu.usbserial-0001` (macOS) uses it for real. Developers: `pip install -e ".[dev]"`
and `python -m pytest`.

## CLI reference

`-c CONFIG` selects the config file. Without it, meshwx uses `$MESHWX_CONFIG`, then
`./config.yaml`, `~/.config/meshwx/config.yaml` and `/etc/meshwx/config.yaml`.

| Command | Purpose |
|---|---|
| `meshwx run [-c CONFIG] [--zip ZIP] [--interval MIN] [--port DEV] [--dry-run] [--log-level L]` | Run the service in the foreground (this is what systemd runs). `--zip` / `--interval` override the config for this run, `--port` the serial port; `--dry-run` uses the terminal instead of a radio; `--log-level` is DEBUG, INFO, WARNING or ERROR. |
| `meshwx preview [-c CONFIG] [--zip ZIP] [--what WHAT]` | Print the messages without sending anything. `WHAT` is a comma-separated list of `current`, `forecast`, `alerts`, `transit`, `broadcast`, `all`. |
| `meshwx send [-c CONFIG] [--port DEV] [--channel N] [--to NODEID] TEXT` | Send one text message: to a channel, or to one node with `--to a1b2c3d4`. On the command line, leave out the node id's `!` or use single quotes (`--to '!a1b2c3d4'`): bash treats `!` specially, even inside double quotes. Stop the service first (`sudo systemctl stop meshwx`): only one program can use the serial port. |
| `meshwx ports` | List serial ports and mark the likely Meshtastic devices. |
| `meshwx set zip ZIP [-c CONFIG]`<br>`meshwx set interval MIN [-c CONFIG]` | Change the home ZIP code or the broadcast interval (saved in `state.json`; a running service picks it up within ~30 s). |
| `meshwx status [-c CONFIG]` | Show the ZIP code and interval in effect and where they come from. |
| `meshwx setup [-c PATH]` | Interactive wizard that writes a config file. It stops before the first question if it cannot write the file; on the Pi, re-run it as shown below. |
| `meshwx example-config` | Print the commented configuration template. |

On the Pi, the `meshwx` command runs these as the service user - see
[The meshwx command on the Pi](#the-meshwx-command-on-the-pi). The service user may read the
config but not change it, so to re-run the wizard for the installed service:

```sh
sudo /opt/meshwx/venv/bin/meshwx setup -c /etc/meshwx/config.yaml
sudo chown root:meshwx /etc/meshwx/config.yaml && sudo chmod 640 /etc/meshwx/config.yaml
sudo systemctl restart meshwx
```

## Troubleshooting

Start with the log: `journalctl -u meshwx -f` (live), `journalctl -u meshwx -n 100`, or
`journalctl -u meshwx --since "1 hour ago"`. For more detail set `log_level: DEBUG` in the config
and restart. The meshtastic library only logs warnings and errors unless you add
`Environment=MESHWX_DEBUG_LIBS=1` under `[Service]` with `sudo systemctl edit meshwx`.
API keys in logged web addresses (query parameters such as `key`, `api_key`, `appID` or
`token`) are replaced by `<redacted>`. With `MESHWX_DEBUG_LIBS=1` the log also holds other
people's mesh messages and node ids and your radio's full settings, including channel keys,
the node's private key and any MQTT password: don't post that output anywhere public.

**The service keeps stopping / "start request repeated too quickly".** After 5 failed starts in
15 minutes systemd gives up instead of restarting forever. Look at the log for the cause (often
a config error, which names the setting), fix it, then:
`sudo systemctl reset-failed meshwx && sudo systemctl start meshwx`

**"Permission denied" on /dev/ttyUSB0 or /dev/ttyACM0.** Serial ports belong to the group
`dialout`. The service user is in it; if you run meshwx or the `meshtastic` tool as yourself,
add yourself too and log in again: `sudo usermod -aG dialout $USER`. Check with
`ls -l /dev/ttyUSB0 /dev/ttyACM0`.

**"Port busy" / "could not exclusively lock port".** Only one program can use the node at a
time. Stop meshwx before using `meshwx send`, `meshwx run` or the `meshtastic` tool, and close
any serial monitor or web flasher. `sudo fuser -v /dev/ttyACM0` (if installed) shows who holds
the port.

**No device found.**

- Use a data cable and the Pi Zero's **USB** port, not **PWR IN**.
- `meshwx ports`, `ls -l /dev/serial/by-id/` and, right after plugging the node in,
  `sudo dmesg | tail -20` show whether Linux sees it.
- A CH340 board that disappears again right after plugging in is usually grabbed by `brltty`
  (a braille display driver on some desktop installs): `sudo apt remove brltty`.
- With several serial devices attached, set `meshtastic.serial_port` to `/dev/meshtastic` or,
  better, to the node's `/dev/serial/by-id/...` path.

**Garbled or failed connection right after plugging in (ModemManager).** On systems with
ModemManager (common with desktop installs) it can probe new serial ports with modem commands.
The udev rule tells it to ignore the boards it knows; for another board copy a line of
`/etc/udev/rules.d/99-meshtastic.rules` with your board's ids (`lsusb`), or, if you have no
cellular modem: `sudo systemctl disable --now ModemManager`.

**Timeouts while connecting to the node.** On connect, the node sends its whole node database,
which takes a long time on a Pi Zero in a big mesh. Set `meshtastic.skip_node_db: true` (log
messages then show node ids instead of names).

**Wrong times / schedule off.** The Pi has no clock battery: after a power cut the clock is
wrong until it gets the time from the network. `timedatectl` should say
`System clock synchronized: yes`; check the time zone too (`sudo raspi-config`, Localisation).
To start meshwx only after the clock is synchronized:
`sudo systemctl enable systemd-time-wait-sync`.

**"Weather unavailable" or old data.** When NWS fails, meshwx falls back to Open-Meteo (the
NWS alert feed is still used when it works), and when both fail it sends a cached report up
to 3 hours old, marked "(cached 25m)" or similar. After a failed refresh it waits 2-10
minutes before trying again, so commands during an outage answer at once from the cache
instead of each waiting for the timeouts. Where a weather station reports no usable
observation, the current conditions come from the NWS hourly forecast and are marked
"fcst" (e.g. "San Juan, PR fcst 4a: ...").
NWS asks API users to identify themselves: put an email address or URL in `weather.contact`.
meshwx sends it in the User-Agent of all its web requests, not only those to NWS.

**Installation problems.**

- `$'\r': command not found`: the scripts got Windows line endings (copied from a Windows PC).
  Clone the repository on the Pi, or fix them: `sed -i 's/\r$//' deploy/*.sh`
- "No space left on device" while installing Python packages: free some space (`df -h`); the
  installer builds in `/var/tmp` because `/tmp` is a small RAM disk on Trixie.
- A download from piwheels.org or pypi.org failed: run the installer again, it continues
  where it stopped.

**Logs and the SD card.** On Trixie the system journal is kept in RAM and lost at reboot
(kinder to SD cards); to keep it across reboots use `sudo raspi-config` (Advanced Options,
Logging). On Bookworm it is written to the SD card; to cap its size create
`/etc/systemd/journald.conf.d/99-size.conf` containing `[Journal]` and `SystemMaxUse=50M`, then
`sudo systemctl restart systemd-journald`. meshwx itself writes `state.json` only when something
changes.

## Updating and uninstalling

Update (keeps your config and state, restarts the service):

```sh
cd ~/meshwx
git pull                     # or copy the new version over the folder
sudo bash deploy/install.sh --upgrade
```

Uninstall:

```sh
sudo bash deploy/uninstall.sh            # keeps /etc/meshwx (config) and /var/lib/meshwx (state)
sudo bash deploy/uninstall.sh --purge    # deletes those too, and the meshwx user
```

## Data sources and terms

| Source | Used for | Terms |
|---|---|---|
| [National Weather Service API](https://www.weather.gov/documentation/services-web-api) (api.weather.gov) | US forecasts, observations, alerts | Free, no key; US government data. Identify yourself in the User-Agent (`weather.contact`). |
| [Open-Meteo](https://open-meteo.com) | Fallback forecasts (worldwide), ZIP lookup fallback | Free for non-commercial use with attribution ([CC BY 4.0](https://open-meteo.com/en/license)), fewer than 10,000 calls per day. Commercial use needs a paid plan. |
| [Zippopotam.us](https://zippopotam.us) | ZIP code to place and coordinates | Open Database License (ODbL); data from GeoNames. |
| Transit providers (MBTA, CTA, TriMet, Sound Transit / OneBusAway, Transitland, 511.org, MTA) | Arrivals | Each has its own terms of use and rate limits; read them when you request a key. Transitland's free plan is non-commercial. |

meshwx is an independent project, not affiliated with or endorsed by Meshtastic LLC, the
National Weather Service or any transit agency. Meshtastic® is a registered trademark of
Meshtastic LLC.

## How it works

One Python process with three threads: the main loop keeps the schedule and the alert timer,
a worker fetches data and answers commands one at a time, and a sender transmits one packet
at a time with a pause in between (alerts first, then replies, then broadcasts). The radio link
reconnects by itself. Weather comes from NWS with Open-Meteo as fallback, ZIP codes are looked
up once and cached, and all text is shortened and split to fit Meshtastic packets.

Details: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) (design, threads, alert rules) and
[docs/CONTRACTS.md](docs/CONTRACTS.md) (module interfaces).

## License

meshwx is released under the [MIT License](LICENSE). The weather, ZIP code and transit data it
fetches stay under each provider's own terms (see [Data sources and terms](#data-sources-and-terms)).

The MIT License covers meshwx's own code and documentation. The sample API responses in
`tests/fixtures/` are third-party data kept for the tests and stay under their providers' terms:
NWS data is a US government work (public domain); Open-Meteo data, including its GeoNames place
data, is [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) ("Weather data by
Open-Meteo.com"); Zippopotam.us data is ODbL (from GeoNames); and the MBTA files contain data
provided by MassDOT under the
[MassDOT Developers License Agreement](https://cdn.mbta.com/sites/default/files/2023-08/mbta-massdot-develop-license-agreement.pdf).
The OneBusAway sample is made up. The capture notes in that folder list each file's source,
capture time and changes.

meshwx uses the [meshtastic](https://github.com/meshtastic/python) Python library, which is
licensed under GPL-3.0 and installed separately by pip.
