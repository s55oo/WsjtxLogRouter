# WsjtxLogRouter

A small, dependency-free GUI router for amateur radio logging. It listens for
QSO events emitted over UDP by **WSJT-X**, **N1MM Logger+** and **DXLog** and
forwards them to several destinations at once:

- raw UDP forward (QLog, JTAlert, GridTracker, HRD, World Radio League, ...)
- Wavelog (self-hosted logbook, HTTP/ADIF)
- CQ Radio (logbook.cqradio.org, HTTP/JSON)
- any generic HTTP endpoint (raw ADIF POST)
- a local ADIF file

Runs on stock Python 3 only (tkinter, socket, ssl, json). No third-party
packages.

```
WsjtxLogRouter.py   main program (GUI)
WsjtxLogRouter.json configuration (auto-loaded on start)
WsjtxLogRouter.log  activity log (written by the router engine)
```

## How it works

                   ┌──────────────────────┐
    WSJT-X  UDP ──►│ 127.0.0.1:2237       │
                   │   (binary WSJT-X     │
                   │    UDP protocol)     │
    N1MM+/DXLog UDP ►│ 127.0.0.1:12060      │  Router (this app)
                   │   (N1MM-style XML      │
                   │    broadcasts)         │
                   └──────────┬───────────┘
                              │ decodes both protocols into ADIF
                              ▼
              ┌───────────────┼────────────────┐
              ▼               ▼                ▼
        UDP forward    HTTP outputs        ADIF file
        (raw datagrams)  │      │
                         ▼      ▼
                     Wavelog  CQ Radio / generic HTTP

- **WSJT-X input (port 2237):** decodes the WSJT-X binary UDP protocol
  (`magic 0xADBCCBDA`, schema-2/schema-3, message types). Only QSO events
  (`QSOLogged`, `LoggedADIF`) are converted to ADIF and dispatched.
- **N1MM+ / DXLog input (port 12060):** listens to N1MM Logger+ or DXLog UDP XML broadcasts
  (`<contactinfo>`, `<contactreplace>`, ...) and converts them to ADIF. DXLog
  must have *Options → Broadcast → Use N1MM QSO format* ticked.
- **UDP forward outputs:** every received datagram is mirrored to the
  configured `host:port` (that is what QLog uses to get the raw WSJT-X
  packets). Replies from forwarded apps are routed back to the sender.
- **Start / Stop** buttons control the listeners; the router can also
  auto-start on launch (and start automatically with Windows — see below).

## Configuration

### Via the GUI

- **Inputs** tab: add/remove UDP listen ports (`WSJTX` default 2237,
  `N1MM` default 12060 — the N1MM input also receives DXLog, which speaks the
  same XML broadcast).
- **Outputs** tab: Add/Edit/Remove. The table shows `Type`, `Target` and
  `Last call`. `Last call` shows the callsign of the most recent QSO that
  was dispatched to that destination.
- **Save config** writes `WsjtxLogRouter.json`.
- Start button turns green while running, Stop button turns red while stopped.

### Via the JSON file

`outputs[]` entries (edit the file, then restart the app — the GUI only
reads the file at launch):

| type | fields | behaviour |
|------|--------|-----------|
| `udp`   | `host`, `port` | raw datagram mirror to host:port (e.g. QLog `127.0.0.1:2240`) |
| `wavelog` | `url`, `key`, `station` | POST `{type:"adif", string, key, station_profile_id}` to the Wavelog v1 API (`.../index.php/api/qso`); logs the HTTP status/body |
| `cqradio` | `url`, `key` | POST the QSO dict as JSON to logbook.cqradio.org (`Authorization: Bearer` + `X-API-KEY`) |
| `http`  | `url`, `key` | POST the raw ADIF text (`Content-Type: text/adif`) to any API |
| `adif`  | `path` | append every logged QSO to a local `.adi` file |

Example:

```json
{
  "inputs": [
    { "type": "wsjtx", "port": 2237 },
    { "type": "n1mm",  "port": 12060 }
  ],
  "outputs": [
    { "type": "udp", "host": "127.0.0.1", "port": 2240, "name": "QLog" },
    {
      "type": "wavelog",
      "name": "Wavelog",
      "url": "http://10.147.17.209:8086/index.php/api/qso",
      "key": "wl...",
      "station": "1"
    },
    {
      "type": "cqradio",
      "name": "CQ Radio",
      "url": "https://logbook.cqradio.org/api/wsjtx/log",
      "key": "..."
    }
  ]
}
```

## Running it

```bat
python WsjtxLogRouter.py
```

- **Auto-start on launch:** the router starts listening ~100 ms after the
  window opens (no need to click Start).
- **Autostart with Windows:** a shortcut named `WsjtxLogRouter.lnk` in the
  Startup folder (`shell:startup`) launches the app at login via
  `pythonw.exe`, so no console window appears. Remove the shortcut to
  disable autostart.

## Notes / behaviour

- **WSJT-X 3.x type numbers:** WSJT-X 3.x sends `schema=3` headers but with
  the plain (schema-2) message type numbers (e.g. `5` = QSOLogged,
  `12` = LoggedADIF, `1` = Status). The decoder accepts both the plain and
  the `+128` extended variants.
- **Duplicate suppression:** WSJT-X sends both `QSOLogged` and `LoggedADIF`
  for a single logged QSO. The router deduplicates on
  call + date + mode + frequency (120 s window) so a destination receives
  each QSO exactly once.
- **ADIF output quality:** QSOs are enriched with `BAND` (derived from
  frequency) and `TX_PWR` (from WSJT-X `tx_pwr`) before dispatch.
- **Logging:** every event/error is written to `WsjtxLogRouter.log`
  (input datagrams, QSO dispatches, HTTP POST results, GUI errors).

## Troubleshooting

- **QSO appears in QLog but not in Wavelog/CQ Radio:** you are running an
  old version of the router (the WSJT-X 3.x type-number fix is required).
  Check `WsjtxLogRouter.log` for `QSO logged` / `-> logged` lines.
- **Nothing is received:** confirm WSJT-X Reporting is enabled with the UDP
  server set to `127.0.0.1:2237`, and that the router shows green Start.
- **All GUI errors** (e.g. a bad config) are captured to
  `WsjtxLogRouter.log` — the app runs headless under `pythonw.exe` and has
  no console to print to.

73 de S55OO