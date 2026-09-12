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
WsjtxLogRouter.exe  standalone executable (bundles Python/Tk; build with the .spec)
WsjtxLogRouter.json configuration (auto-loaded on start)
WsjtxLogRouter.log  activity log (written by the router engine)
WsjtxLogRouter.spec PyInstaller build file (+ manifest.xml)
docs/               screenshots used by this README
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

## Logger setup

The router itself needs no configuration beyond an `n1mm` input on port
12060 — it is the *logger* that must be told to broadcast there. Both N1MM+
and DXLog speak the same XML format on UDP 12060, so the router can listen to
either (or both, on different ports).

### N1MM Logger+

**Config → Config Ports… → Broadcast Data**, enable:

- **Contacts** – sends a `contactinfo` packet when a QSO is logged (the QSO
  the router forwards).
- **External Callsign Lookup** – optional `lookupinfo` packets.

Destination **`127.0.0.1:12060`** (when the router runs on the same PC).

### DXLog.net

Under **Options → Broadcast** tick:

- **Use N1MM QSO format** – the XML layout the router parses (this is what
  makes DXLog work — no router changes needed).
- **QSOs** – sends a `contactinfo` packet when a QSO is logged.
- **Callsign on space or tab** – optional pre-log `lookupinfo` packets.

The broadcast target defaults to `127.0.0.1:12060` (`Network_QSOsBroadcastPort`
in DXLog's config) — leave it as-is when the router runs on the same PC.
Verified against DXLog.net v2.6.34.

![DXLog.net – Options → Broadcast](docs/dxlog-broadcast-setup.png)

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
WsjtxLogRouter.exe          # standalone executable (no Python needed)
:: or from source:
python WsjtxLogRouter.py
```

- **Auto-start on launch:** the router starts listening ~100 ms after the
  window opens (no need to click Start).
- **Autostart with Windows:** a shortcut named `WsjtxLogRouter.lnk` in the
  Startup folder (`shell:startup`) launches the EXE at login. Remove the
  shortcut to disable autostart. The EXE and `WsjtxLogRouter.json` must stay
  in the same folder (config is read/written next to the program).
- **Building the EXE** (PyInstaller is already installed):

  ```bat
  python -m PyInstaller --clean --noconfirm WsjtxLogRouter.spec
  copy /Y dist\WsjtxLogRouter.exe WsjtxLogRouter.exe
  ```

  The spec produces a single windowed EXE with the manifest embedded;

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