# WsjtxLogRouter

A small, dependency-free GUI router for amateur radio logging. It listens for
QSO events emitted over UDP by **WSJT-X**, **N1MM Logger+** and **DXLog** and
forwards them to several destinations at once:

- raw UDP forward (QLog, JTAlert, GridTracker, World Radio League, ...)
- Ham Radio Deluxe Logbook (N1MM-style UDP broadcast, "QSO Forwarding")
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
    N1MM+/DXLog UDP ►│ 0.0.0.0:12060       │  Router (this app)
                   │   (N1MM-style XML     │
                   │    broadcasts)        │
                   └──────────┬───────────┘
                              │ decodes both protocols into ADIF
                              ▼
              ┌───────────────┼───────────────┬────────────────┐
              ▼               ▼               ▼                ▼
        UDP forward      HRD output     HTTP outputs        ADIF file
        (raw datagrams)  (N1MM XML,       │      │
                          synthesised)     ▼      ▼
                                       Wavelog  CQ Radio / generic HTTP

- **WSJT-X input (port 2237):** decodes the WSJT-X binary UDP protocol
  (`magic 0xADBCCBDA`, schema-2/schema-3, message types). Only QSO events
  (`QSOLogged`, `LoggedADIF`) are converted to ADIF and dispatched. Bound to
  `127.0.0.1` (WSJT-X only ever talks to the router over loopback on the
  same PC).
- **N1MM+ / DXLog input (port 12060):** listens to N1MM Logger+ or DXLog UDP XML broadcasts
  (`<contactinfo>`, `<contactreplace>`, ...) and converts them to ADIF. DXLog
  must have *Options → Broadcast → Use N1MM QSO format* ticked. Bound to
  `0.0.0.0` (all interfaces) so it also receives LAN subnet broadcasts, not
  just loopback unicast.
- **UDP forward outputs:** every received datagram is mirrored to the
  configured `host:port` (that is what QLog uses to get the raw WSJT-X
  packets). Replies from forwarded apps are routed back to the sender.
  QSOs that originate from N1MM+ / DXLog have no binary WSJT-X packet to
  mirror, so the router *synthesises* a WSJT-X `QSOLogged` datagram from the
  decoded ADIF and sends that instead — meaning N1MM/DXLog contacts also land
  in apps that only speak the WSJT-X UDP protocol (e.g. QLog).
- **HRD output:** every QSO (regardless of input) is *also* re-encoded as
  an N1MM-style `<contactinfo>` UDP broadcast for Ham Radio Deluxe
  Logbook's QSO Forwarding — see "Ham Radio Deluxe" below.
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

Destination **`127.0.0.1:12060`** when the router runs on the same PC, or the
PC's LAN broadcast (e.g. `192.168.0.255:12060`) — the router listens on
`0.0.0.0` (all interfaces), so it receives loopback unicast *and* subnet
broadcasts on any adapter.

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

## Ham Radio Deluxe

Add an `hrd` output (`host`/`port`, default port `2333`) and every QSO the
router handles — from WSJT-X, N1MM+ or DXLog alike — is re-broadcast as an
N1MM-style `<contactinfo>` UDP packet, which is exactly what HRD Logbook's
**QSO Forwarding → UDP Receive** consumes. On the HRD side: **Logbook →
Tools → QSO Forwarding**, tick **UDP Receive**, set the port to match (2333
by default), leave the IP as `127.0.0.1`.

**Same PC as HRD:** set the `hrd` output's `host` to `127.0.0.1` — done.

**HRD on a different PC (e.g. connected via ZeroTier or another VPN/LAN):**
this is *not* guaranteed to work. HRD's own documentation describes UDP
Receive strictly in terms of localhost (127.0.0.1) with no mention of a
remote/different computer, which suggests it may only ever bind its
listening socket to loopback — in which case a packet arriving over
ZeroTier (addressed to the PC's ZeroTier IP, not `127.0.0.1`) would never
reach it, no matter how the sender is configured. ZeroTier itself is not
the obstacle here (unicast UDP across a ZeroTier network behaves like
ordinary LAN UDP once both peers are online), so if pointing the `hrd`
output straight at the HRD PC's ZeroTier IP doesn't show up in HRD, don't
keep tweaking the network — use the relay pattern instead:

1. Run a second, minimal instance of WsjtxLogRouter **on the HRD PC**.
2. Give it an `n1mm` input bound to `0.0.0.0` on some free port (inputs
   already bind all interfaces, so this receives the forwarded packet from
   the remote PC over ZeroTier).
3. Give it an `hrd` output pointed at `127.0.0.1:2333` (loopback, same PC
   as HRD) — satisfies HRD's own expectation regardless of how its socket
   is actually bound.

Either way, also check Windows Firewall on the HRD PC: ZeroTier's virtual
adapter is often classified as a **Public** network, which blocks
unsolicited inbound UDP by default — set it to **Private**, or add an
explicit inbound rule for the port, if packets seem to vanish.

## Configuration

### Via the GUI

The window opens in a **minimal view**: the current Running/Stopped state,
the last QSO logged (callsign + time), and a Destinations list showing
every configured output with a status dot and its last call. Click
**Details ▸** to switch to the full view with editable Inputs/Outputs
tables and the activity log; click **Minimal ◂** to switch back. Both
views share the same underlying data, so edits made in Details show up in
the minimal Destinations list immediately.

- **Status dot** per destination: green ● once an HTTP output's last POST
  actually succeeded, red ● once it failed (checked against the real HTTP
  response, not just "a send was attempted"), gray ○ for UDP/ADIF outputs
  (fire-and-forget — there is no delivery confirmation to check).
- **Inputs** tab (Details view): add/remove UDP listen ports (`WSJTX`
  default 2237, `N1MM` default 12060 — the N1MM input also receives DXLog,
  which speaks the same XML broadcast).
- **Outputs** tab (Details view): Add/Edit/Remove. The table shows `Type`,
  `Target`, the status dot and `Last call` — the callsign of the most
  recent QSO dispatched to that destination.
- **Save config** writes `WsjtxLogRouter.json`.
- Start button turns green while running, Stop button turns red while stopped.

### Via the JSON file

`outputs[]` entries (edit the file, then restart the app — the GUI only
reads the file at launch):

| type | fields | behaviour |
|------|--------|-----------|
| `udp`   | `host`, `port` | raw datagram mirror to host:port (e.g. QLog `127.0.0.1:2240`); for N1MM/DXLog QSOs a synthesized WSJT-X `QSOLogged` packet is sent instead |
| `hrd`   | `host`, `port` | every logged QSO (from *any* input) re-encoded as an N1MM-style `<contactinfo>` UDP broadcast, for Ham Radio Deluxe Logbook's **QSO Forwarding → UDP Receive** (default port `2333`) — see "Ham Radio Deluxe" below |
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
    { "type": "hrd", "host": "127.0.0.1", "port": 2333, "name": "HRD" },
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
  each QSO exactly once. Frequency is compared numerically
  (`round(float(freq), 6)`), not as a raw string — WSJT-X's own ADIF keeps a
  fixed 6-decimal frequency (e.g. `14.075080`) while the router's own
  QSOLogged-derived ADIF strips trailing zeros (`14.07508`) for the same
  QSO; comparing those as plain strings used to let the pair slip past
  dedup and double-post to every HTTP output whenever a frequency happened
  to end in a zero.
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
- **Every QSO posts twice to Wavelog/CQ Radio/HTTP:** fixed in 1.2.0 (see
  Duplicate suppression above) — update if you're on an older build.
- **HRD never receives anything:** check `WsjtxLogRouter.log` for
  `Output: HRD '<name>' -> host:port` at startup (confirms the output is
  configured) — the `hrd` output is fire-and-forget UDP, so there is no
  error logged if nothing is listening on the other end. If HRD is on a
  different PC, see "Ham Radio Deluxe" above (loopback-only limitation +
  firewall).
- **A destination's status dot stays red:** an HTTP output's last POST
  failed; check `WsjtxLogRouter.log` for the `HTTP ERROR '<name>': ...`
  line for the actual response/reason.
- **All GUI errors** (e.g. a bad config) are captured to
  `WsjtxLogRouter.log` — the app runs headless under `pythonw.exe` and has
  no console to print to.

73 de S55OO