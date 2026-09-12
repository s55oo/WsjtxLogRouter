#!/usr/bin/env python3
"""
WSJT-X Multi-Logger Router (GUI)
=================================
Listen on one or more localhost UDP ports for WSJT-X, then route each event to
several outputs at once:

  * UDP forward hosts   - raw datagrams to GridTracker, Hamclock, N1MM+,
                          WRL, etc. (bidirectional, replies rout back to WSJT-X)
  * CQ Radio HTTP       - logged QSOs as JSON to logbook.cqradio.org
  * Generic HTTP ADIF   - logged QSOs as raw ADIF POST to any API
  * ADIF file           - append every logged QSO to a local .adi file

Only standard library is used (tkinter, socket, ssl, json). Config is saved to
WsjtxLogRouter.json next to this script.

Usage:
    python3 WsjtxLogRouter.py
"""

import json
import re
import socket
import ssl
import struct
import threading
import time
import queue
import sys
import os
import traceback
import xml.etree.ElementTree as ET
from datetime import datetime, date, timedelta
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError

__version__ = "1.0.0"

if getattr(sys, "frozen", False):
    _BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
else:
    _BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(_BASE_DIR, "WsjtxLogRouter.json")

LOG_FILE = os.path.join(_BASE_DIR, "WsjtxLogRouter.log")
MAGIC = 0xADBCCBDA

HTTP_TIMEOUT = 15
UDP_TIMEOUT = 60.0
RECV_BUFSIZE = 65535

SCHEMA2_NAMES = {
    0: "Heartbeat", 1: "Status", 2: "Decode", 3: "Clear", 4: "Reply",
    5: "QSOLogged", 6: "Close", 7: "Replay", 8: "HaltTx", 9: "FreeText",
    10: "WSPRDecode", 11: "Location", 12: "LoggedADIF", 13: "HighlightCallsign",
    14: "SwitchConfiguration", 15: "Configure",
}
# WSJT-X 3.x sends schema=3 headers but with the plain (schema-2) type
# numbers for most payloads; older wsjt-x also used the +128 extended
# variants. Accept both.
SCHEMA3_NAMES = dict(SCHEMA2_NAMES)
SCHEMA3_NAMES.update({t + 128: n for t, n in SCHEMA2_NAMES.items()})


# ─── WSJT-X UDP decoding ────────────────────────────────────────

class Reader:
    def __init__(self, data):
        self.buf = data
        self.pos = 0

    def remaining(self):
        return self.buf[self.pos:]

    def peek_len(self):
        try:
            return struct.unpack(">i", self.buf[self.pos:self.pos + 4])[0]
        except struct.error:
            return None

    def take(self, n):
        b = self.buf[self.pos:self.pos + n]
        self.pos += n
        return b

    def u8(self):
        return struct.unpack(">B", self.take(1))[0]

    def i32(self):
        return struct.unpack(">i", self.take(4))[0]

    def u32(self):
        return struct.unpack(">I", self.take(4))[0]

    def u64(self):
        return struct.unpack(">Q", self.take(8))[0]

    def i64(self):
        return struct.unpack(">q", self.take(8))[0]

    def boolean(self):
        return self.take(1) != b"\x00"

    def f64(self):
        return struct.unpack(">d", self.take(8))[0]

    def string(self):
        n = self.i32()
        if n == -1:
            return None
        return self.take(n).decode("utf-8", errors="replace")

    def qtime(self):
        ms = self.u32()
        if ms == 0xFFFFFFFF:
            return ""
        sec, ms = divmod(ms, 1000)
        m, sec = divmod(sec, 60)
        h, m = divmod(m, 60)
        return f"{h:02d}:{m:02d}:{sec:02d}"

    def qdatetime(self):
        days = self.i64()
        ms = self.u32()
        spec = self.u8()
        d = date(1970, 1, 1) + timedelta(days=days - 2440588)
        sec, ms = divmod(ms, 1000)
        m, sec = divmod(sec, 60)
        h, m = divmod(m, 60)
        return f"{d.isoformat()} {h:02d}:{m:02d}:{sec:02d}"


def try_parse_rest(r, parsers):
    save = r.pos
    vals = []
    try:
        for p in parsers:
            vals.append(p())
        if not r.remaining():
            return vals
    except (struct.error, IndexError, ValueError):
        pass
    r.pos = save
    return None


def decode_datagram(data):
    """Return (type_name, decoded_dict) or (None, None) for non-WSJT-X data."""
    if len(data) < 12:
        return None, None
    magic, schema, mtype = struct.unpack(">III", data[:12])
    if magic != MAGIC:
        return None, None
    names = SCHEMA2_NAMES if schema == 2 else SCHEMA3_NAMES
    name = names.get(mtype, "Unknown")
    r = Reader(data[12:])
    d = {}
    try:
        d["id"] = r.string()
        if mtype in (5, 133):                       # QSOLogged
            d["date_off"] = r.qdatetime()
            d["call"] = r.string()
            d["grid"] = r.string()
            d["freq_hz"] = r.u64()
            d["mode"] = r.string()
            d["rst_sent"] = r.string()
            d["rst_rcvd"] = r.string()
            d["tx_pwr"] = r.string()
            d["comments"] = r.string()
            d["name"] = r.string()
            d["date_on"] = r.qdatetime()
            d["operator"] = r.string()
            d["my_call"] = r.string()
            d["my_grid"] = r.string()
            d["exch_sent"] = r.string()
            d["exch_rcvd"] = r.string()
        elif mtype in (12, 140):                    # LoggedADIF
            d["adif"] = r.string()
        elif mtype in (1, 129):                     # Status
            d["freq_hz"] = r.u64()
            d["mode"] = r.string()
            d["dx_call"] = r.string()
            d["report"] = r.string()
            d["tx_mode"] = r.string()
            d["tx_enabled"] = r.boolean()
            d["transmitting"] = r.boolean()
            d["decoding"] = r.boolean()
            d["rx_df"] = r.i32()
            d["tx_df"] = r.i32()
            d["de_call"] = r.string()
            d["de_grid"] = r.string()
            d["dx_grid"] = r.string()
            d["tx_watchdog"] = r.boolean()
            d["sub_mode"] = r.string()
            d["fast_mode"] = r.boolean()
            d["special_op"] = r.u8()
            ext = try_parse_rest(r, [r.u32, r.u32, r.string, r.string])
            if ext:
                d["freq_tol"] = ext[0]
                d["tr_period"] = ext[1]
                d["config_name"] = ext[2]
                d["tx_message"] = ext[3]
        elif mtype in (2, 130):                     # Decode
            d["new"] = r.boolean()
            d["time"] = r.qtime()
            d["snr"] = r.i32()
            d["dt_s"] = r.f64()
            d["df_hz"] = r.u32()
            d["mode"] = r.string()
            d["message"] = r.string()
            d["low_conf"] = r.boolean()
            d["off_air"] = r.boolean()
        elif mtype in (0, 128):                     # Heartbeat
            ext = try_parse_rest(r, [r.u32, r.string, r.string])
            if ext:
                d["max_schema"] = ext[0]
                d["version"] = ext[1]
                d["revision"] = ext[2]
    except (struct.error, IndexError, ValueError):
        return None, None
    return name, d


def parse_adif(adif_str):
    """Parse ADIF text into a dict of field:value pairs."""
    fields = {}
    if not adif_str:
        return fields
    for match in re.finditer(r"<(\w+):(\d+)(?::\w)?>", adif_str, re.IGNORECASE):
        name = match.group(1).upper()
        length = int(match.group(2))
        start = match.end()
        value = adif_str[start:start + length].strip()
        if value:
            fields[name] = value
    return fields


def adif_to_qso(fields):
    date_raw = fields.get("QSO_DATE", "")
    qso_date = f"{date_raw[:4]}-{date_raw[4:6]}-{date_raw[6:8]}" if len(date_raw) >= 8 else ""
    time_raw = fields.get("TIME_ON", "")
    if len(time_raw) >= 6:
        time_on = f"{time_raw[:2]}:{time_raw[2:4]}:{time_raw[4:6]}"
    elif len(time_raw) >= 4:
        time_on = f"{time_raw[:2]}:{time_raw[2:4]}:00"
    else:
        time_on = ""
    return {
        "callsign": fields.get("CALL", ""),
        "band": fields.get("BAND", ""),
        "mode": fields.get("MODE", ""),
        "submode": fields.get("SUBMODE", ""),
        "frequency": fields.get("FREQ", ""),
        "qso_date": qso_date,
        "time_on": time_on,
        "rst_sent": fields.get("RST_SENT", ""),
        "rst_rcvd": fields.get("RST_RCVD", ""),
        "grid_square": fields.get("GRIDSQUARE", ""),
        "name": fields.get("NAME", ""),
        "comment": fields.get("COMMENT", ""),
        "tx_power": fields.get("TX_PWR", ""),
        "country": fields.get("COUNTRY", ""),
        "dxcc": fields.get("DXCC", ""),
        "my_callsign": fields.get("STATION_CALLSIGN", fields.get("OPERATOR", "")),
        "my_grid": fields.get("MY_GRIDSQUARE", ""),
    }


def adif_field(name, value):
    if value is None or str(value) == "":
        return ""
    value = str(value)
    return f"<{name}:{len(value)}>{value}"


BAND_EDGES = [
    (1.8, 2.0, "160M"), (3.5, 4.0, "80M"), (5.25, 5.45, "60M"),
    (7.0, 7.3, "40M"), (10.1, 10.15, "30M"), (14.0, 14.35, "20M"),
    (18.068, 18.168, "17M"), (21.0, 21.45, "15M"), (24.89, 24.99, "12M"),
    (28.0, 29.7, "10M"), (50.0, 54.0, "6M"), (70.0, 71.0, "4M"),
    (144.0, 148.0, "2M"), (220.0, 225.0, "1.25M"), (430.0, 440.0, "70CM"),
    (902.0, 928.0, "33CM"), (1240.0, 1300.0, "23CM"),
]


def band_from_freq(hz):
    if not hz:
        return ""
    try:
        mhz = float(hz) / 1e6
    except (TypeError, ValueError):
        return ""
    for lo, hi, band in BAND_EDGES:
        if lo <= mhz <= hi:
            return band
    return ""


def qsol_logged_to_adif(d):
    ts = d.get("date_off") or ""
    parts = [adif_field("QSO_DATE", ts[:10].replace("-", ""))]
    if len(ts) > 11:
        parts.append(adif_field("TIME_ON", ts[11:19].replace(":", "")))
    parts.append(adif_field("CALL", d.get("call")))
    parts.append(adif_field("MODE", d.get("mode")))
    fr = d.get("freq_hz")
    if fr:
        parts.append(adif_field("FREQ", ("%.6f" % (fr / 1e6)).rstrip("0").rstrip(".")))
        parts.append(adif_field("BAND", band_from_freq(fr)))
    parts.append(adif_field("GRIDSQUARE", d.get("grid")))
    parts.append(adif_field("RST_SENT", d.get("rst_sent")))
    parts.append(adif_field("RST_RCVD", d.get("rst_rcvd")))
    parts.append(adif_field("NAME", d.get("name")))
    parts.append(adif_field("COMMENT", d.get("comments")))
    parts.append(adif_field("TX_PWR", d.get("tx_pwr")))
    parts.append(adif_field("STATION_CALLSIGN", d.get("my_call")))
    parts.append(adif_field("OPERATOR", d.get("operator")))
    parts.append(adif_field("MY_GRIDSQUARE", d.get("my_grid")))
    parts.append(adif_field("STX_STRING", d.get("exch_sent")))
    parts.append(adif_field("SRX_STRING", d.get("exch_rcvd")))
    return "\n".join(p for p in parts if p) + "\n<EOR>"


def decode_n1mm(data):
    """Decode an N1MM Logger+ UDP XML datagram -> (message_name, field_dict)."""
    try:
        text = data.decode("utf-8", errors="replace")
    except Exception:
        return None, None
    pos = text.find("<")
    if pos > 0:
        text = text[pos:]
    try:
        root = ET.fromstring(text)
    except Exception:
        return None, None
    tag = root.tag.split("}")[-1].lower()
    d = {child.tag.split("}")[-1].lower(): (child.text or "").strip()
         for child in root}
    return tag, d


def n1mm_to_adif(d):
    ts = d.get("timestamp") or ""
    parts = [adif_field("QSO_DATE", ts[:10].replace("-", ""))]
    if len(ts) > 10:
        parts.append(adif_field("TIME_ON", ts[11:19].replace(":", "")))
    parts.append(adif_field("CALL", d.get("call")))
    parts.append(adif_field("MODE", d.get("mode")))
    try:
        rxfreq_hz = int(d.get("rxfreq") or 0) * 10      # N1MM sends 10 Hz units
    except (TypeError, ValueError):
        rxfreq_hz = 0
    if rxfreq_hz:
        parts.append(adif_field("FREQ", ("%.6f" % (rxfreq_hz / 1e6)).rstrip("0").rstrip(".")))
        parts.append(adif_field("BAND", band_from_freq(rxfreq_hz)))
    parts.append(adif_field("GRIDSQUARE", d.get("gridsquare")))
    parts.append(adif_field("RST_SENT", d.get("snt")))
    parts.append(adif_field("RST_RCVD", d.get("rcv")))
    parts.append(adif_field("NAME", d.get("name")))
    parts.append(adif_field("COMMENT", d.get("comment")))
    parts.append(adif_field("STATION_CALLSIGN", d.get("mycall")))
    parts.append(adif_field("OPERATOR", d.get("operator")))
    parts.append(adif_field("TX_PWR", d.get("power")))
    parts.append(adif_field("CQZ", d.get("zone")))
    parts.append(adif_field("ARRL_SECT", d.get("section")))
    return "\n".join(p for p in parts if p) + "\n<EOR>"


# ─── Engine ──────────────────────────────────────────────────────

class Router:
    def __init__(self, logfn, qsofn=None):
        self.logfn = logfn
        self.qsofn = qsofn
        self.inputs = []               # list of dict {type: wsjtx|n1mm, port} (dxlog -> n1mm)
        self.udp_outputs = []          # list of (host, port)
        self.http_outputs = []         # list of dict {name, url, key, cqradio:bool}
        self.adif_path = None
        self.running = False
        self._sockets = {}
        self._threads = {}
        self._kind = {}                # port -> input type
        self._mapping = {}             # forward addr -> {client: last_seen}
        self._recent_qsos = {}         # fingerprint -> expiry, dedupe WSJT-X double events
        self._last_call = {}           # output key -> last callsign sent there

    def configure(self, cfg):
        self.inputs = []
        for item in cfg.get("inputs", []):
            if isinstance(item, dict):
                self.inputs.append({
                    "type": "n1mm" if item.get("type") == "dxlog"
                            else item.get("type", "wsjtx"),
                    "port": int(item.get("port", 2237))})
            else:
                self.inputs.append({"type": "wsjtx", "port": int(item)})
        self.udp_outputs = []
        self.http_outputs = []
        self.adif_path = None
        for out in cfg.get("outputs", []):
            t = out.get("type")
            if t == "udp":
                self.udp_outputs.append((out["host"], int(out["port"])))
            elif t == "cqradio":
                self.http_outputs.append({
                    "name": out.get("name", "CQ Radio"),
                    "url": out.get("url", "https://logbook.cqradio.org/api/wsjtx/log"),
                    "key": out.get("key", ""), "cqradio": True,
                })
            elif t == "wavelog":
                self.http_outputs.append({
                    "name": out.get("name", "Wavelog"),
                    "url": out.get("url", ""),
                    "key": out.get("key", ""),
                    "station": str(out.get("station", "")).strip(),
                    "wavelog": True,
                })
            elif t == "http":
                self.http_outputs.append({
                    "name": out.get("name", "HTTP logger"),
                    "url": out.get("url", ""),
                    "key": out.get("key", ""), "cqradio": False,
                })
            elif t == "adif":
                self.adif_path = out.get("path", "")

    def _log(self, msg):
        self.logfn(msg)
        try:
            with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
        except OSError:
            pass

    def _emit_qso(self, call):
        """Notify the GUI of the last callsign dispatched to each output."""
        if not call or not self.qsofn:
            return
        for o in self.http_outputs:
            key = "http:" + o["url"]
            self._last_call[key] = call
            try:
                self.qsofn((key, call))
            except Exception:
                pass
        for host, port in self.udp_outputs:
            key = "udp:%s:%s" % (host, port)
            self._last_call[key] = call
            try:
                self.qsofn((key, call))
            except Exception:
                pass
        if self.adif_path:
            key = "adif:" + self.adif_path
            self._last_call[key] = call
            try:
                self.qsofn((key, call))
            except Exception:
                pass

    # ---- lifecycle ----
    def start(self):
        if self.running:
            return
        self.running = True
        self._mapping = {}
        self._kind = {}
        for inp in self.inputs:
            port, kind = inp["port"], inp["type"]
            self._kind[port] = kind
            try:
                sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                if hasattr(socket, "SO_REUSEPORT"):
                    try:
                        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                    except OSError:
                        pass
                sock.bind(("127.0.0.1", port))
                sock.settimeout(0.5)
                self._sockets[port] = sock
                th = threading.Thread(target=self._rx_loop, args=(port, sock), daemon=True)
                self._threads[port] = th
                th.start()
                self._log(f"Input: {kind.upper()} listening on UDP 127.0.0.1:{port}")
            except OSError as e:
                self._log(f"ERROR: cannot bind port {port}: {e}")
                self.running = False
                return
            except OSError as e:
                self._log(f"ERROR: cannot bind port {port}: {e}")
                self.running = False
                return
        for host, port in self.udp_outputs:
            self._log(f"Output: UDP forward to {host}:{port}")
        for o in self.http_outputs:
            self._log(f"Output: HTTP '{o['name']}' -> {o['url']}"
                      + (" (API key set)" if o["key"] else " (no key)"))
        if self.adif_path:
            self._log(f"Output: ADIF file -> {self.adif_path}")
        self._log("Router started.")

    def stop(self):
        if not self.running:
            return
        self.running = False
        for sock in self._sockets.values():
            try:
                sock.close()
            except OSError:
                pass
        self._sockets = {}
        self._threads = {}
        self._log("Router stopped.")

    # ---- RX loop ----
    def _rx_loop(self, port, sock):
        while self.running:
            try:
                data, src = sock.recvfrom(RECV_BUFSIZE)
            except socket.timeout:
                continue
            except OSError:
                break
            self._handle(port, data, src)

    def _handle(self, port, data, src):
        if self._kind.get(port) == "n1mm":
            self._handle_n1mm(port, data, src)
            return
        if src in self.udp_outputs:
            # datagram from a forward app -> route replies back to clients
            clients = self._mapping.get(src)
            if clients:
                for client in list(clients):
                    try:
                        self._sockets[port].sendto(data, client)
                    except OSError:
                        pass
            return
        self._log(f"[in] port {port} <- {src[0]}:{src[1]} ({len(data)} B)")
        # fan out raw datagrams to UDP forwards and remember origin
        now = time.time()
        for fwd in self.udp_outputs:
            try:
                self._sockets[port].sendto(data, fwd)
                self._mapping.setdefault(fwd, {})[src] = now
            except OSError as e:
                self._log(f"ERROR: can't send to {fwd[0]}:{fwd[1]}: {e}")
        # content-based routing
        name, d = decode_datagram(data)
        if (name == "QSOLogged"):
            self._log(f"QSO logged: {d.get('call')} {d.get('grid', '')} "
                      f"{d.get('mode', '')} @ {d.get('date_off', '')}")
            self._dispatch_adif(qsol_logged_to_adif(d))
        elif name == "LoggedADIF":
            self._dispatch_adif(d.get("adif") or "")

    def _handle_n1mm(self, port, data, src):
        name, d = decode_n1mm(data)
        if not name:
            return
        if name in ("contactinfo", "contactreplace"):
            self._log(f"[n1mm:{port}] {name}: {d.get('call', '')} "
                      f"{d.get('mode', '')} @ {d.get('timestamp', '')}")
            self._dispatch_adif(n1mm_to_adif(d))
        elif name == "contactdelete":
            self._log(f"[n1mm:{port}] contact deleted {d.get('call', '')}")
        elif name in ("radioinfo", "appinfo", "lookupinfo", "spot"):
            pass

    def _dispatch_adif(self, adif):
        if not adif or not adif.strip():
            return
        fields = parse_adif(adif)
        qso = adif_to_qso(fields)
        key = (qso.get("callsign", "").strip().upper(),
               qso.get("qso_date", ""),
               qso.get("mode", "").strip().upper(),
               qso.get("frequency", "").strip())
        now = time.time()
        if key[0] and key in self._recent_qsos and self._recent_qsos[key] > now:
            self._log(f"  !! duplicate {key[0]} {key[1]} {key[2]} - skipped")
            return
        if key[0]:
            self._recent_qsos[key] = now + 120
            for dead in [k for k, t in self._recent_qsos.items() if t <= now]:
                del self._recent_qsos[dead]
        call = qso.get("callsign") or ""
        self._log(f"  -> logged {call} {qso.get('band', '')} "
                  f"{qso.get('mode', '')} {qso.get('frequency', '')}")
        self._emit_qso(call)
        self._dispatch_http(adif, qso)
        self._append_adif(adif)

    def _dispatch_http(self, adif, qso):
        if not self.http_outputs:
            return
        for o in self.http_outputs:
            threading.Thread(target=self._post, args=(o, adif, qso), daemon=True).start()

    def _post(self, o, adif, qso):
        try:
            if o.get("wavelog"):
                payload = json.dumps(self._wavelog_payload(adif, o)).encode("utf-8")
                content_type = "application/json"
            elif o["cqradio"]:
                payload = json.dumps(qso).encode("utf-8")
                content_type = "application/json"
            else:
                payload = adif.encode("utf-8")
                content_type = "text/adif"
            req = Request(o["url"], data=payload, method="POST")
            req.add_header("Content-Type", content_type)
            if o["key"] and not o.get("wavelog"):
                req.add_header("Authorization", "Bearer " + o["key"])
                req.add_header("X-API-KEY", o["key"])
            ctx = ssl.create_default_context()
            resp = urlopen(req, context=ctx, timeout=HTTP_TIMEOUT)
            body = resp.read().decode("utf-8", errors="replace")[:500]
            self._log(f"  HTTP OK '{o['name']}': {resp.status}"
                      + (f" body={body}" if o.get("wavelog") else ""))
        except HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", errors="replace")[:500]
            except Exception:
                pass
            self._log(f"  HTTP ERROR '{o['name']}': {e.code}"
                      + (f" body={body}" if o.get("wavelog") else ""))
        except URLError as e:
            self._log(f"  HTTP ERROR '{o['name']}': {e.reason}")
        except Exception as e:
            self._log(f"  HTTP ERROR '{o['name']}': {e}")

    @staticmethod
    def _wavelog_payload(adif, o):
        p = {"type": "adif", "string": adif}
        if o.get("key"):
            p["key"] = o["key"]
        if o.get("station"):
            p["station_profile_id"] = o["station"]
        return p

    def _append_adif(self, adif):
        if not self.adif_path:
            return
        try:
            with open(self.adif_path, "a", encoding="utf-8") as f:
                f.write(adif if adif.endswith("\n") else adif + "\n")
        except OSError as e:
            self._log(f"  ADIF file error: {e}")


# ─── GUI ─────────────────────────────────────────────────────────

class App:
    def __init__(self, root):
        self.root = root
        self.root.title(f"WSJT-X Multi-Logger Router v{__version__}")
        self.root.geometry("760x560")
        self.log_q = queue.Queue()
        self.qso_q = queue.Queue()
        self.router = Router(self.log_q.put, self.qso_q.put)
        self._last_calls = {}          # output key -> last callsign sent

        import tkinter as tk
        from tkinter import ttk, messagebox
        self.tk = tk
        self.ttk = ttk
        self.messagebox = messagebox

        top = ttk.Frame(root, padding=8)
        top.pack(fill="x")

        self.start_btn = tk.Button(top, text="Start", command=self.on_start,
                                   relief="raised", bd=1)
        self.start_btn.pack(side="left", padx=(0, 4))
        self.stop_btn = tk.Button(top, text="Stop", command=self.on_stop,
                                  relief="raised", bd=1)
        self.stop_btn.pack(side="left", padx=4)
        ttk.Button(top, text="Save config", command=self.on_save).pack(side="left", padx=(4, 0))

        panes = ttk.PanedWindow(root, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=(4, 0))

        # ---- inputs ----
        left = ttk.Frame(panes)
        ttk.Label(left, text="UDP Inputs (listen ports)").pack(anchor="w")
        self.in_tree = ttk.Treeview(left, columns=("kind", "port"), height=8, show="headings")
        self.in_tree.heading("kind", text="Type")
        self.in_tree.heading("port", text="Listen port")
        self.in_tree.column("kind", width=70)
        self.in_tree.column("port", width=90, anchor="center")
        self.in_tree.pack(fill="both", expand=True, pady=2)
        ib = ttk.Frame(left)
        ttk.Button(ib, text="Add", command=self.add_input).pack(side="left")
        ttk.Button(ib, text="Remove", command=self.del_input).pack(side="left", padx=4)
        ib.pack(anchor="w")
        panes.add(left, weight=1)

        # ---- outputs ----
        right = ttk.Frame(panes)
        ttk.Label(right, text="Outputs").pack(anchor="w")
        self.out_tree = ttk.Treeview(
            right, columns=("type", "target", "last"), height=9, show="headings")
        self.out_tree.heading("type", text="Type")
        self.out_tree.heading("target", text="Target")
        self.out_tree.heading("last", text="Last call")
        self.out_tree.column("type", width=90)
        self.out_tree.column("target", width=200)
        self.out_tree.column("last", width=100, anchor="center")
        self.out_tree.pack(fill="both", expand=True, pady=2)
        ob = ttk.Frame(right)
        ttk.Button(ob, text="Add", command=self.add_output).pack(side="left")
        ttk.Button(ob, text="Edit", command=self.edit_output).pack(side="left", padx=4)
        ttk.Button(ob, text="Remove", command=self.del_output).pack(side="left", padx=4)
        ob.pack(anchor="w")
        panes.add(right, weight=4)

        # ---- log ----
        ttk.Label(root, text="Activity log").pack(anchor="w", padx=8, pady=(6, 0))
        self.log_text = tk.Text(root, height=12, state="disabled", wrap="word")
        self.log_text.pack(fill="both", expand=True, padx=8, pady=4)

        self.load_config()
        self.render()
        root.after(150, self.poll_log)
        root.protocol("WM_DELETE_WINDOW", self.on_close)
        root.after(100, self.on_start)

    # ---- helpers ----
    def log(self, msg):
        self.log_q.put(str(msg))

    def poll_log(self):
        while True:
            try:
                msg = self.log_q.get_nowait()
            except queue.Empty:
                break
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{datetime.now().strftime('%H:%M:%S')}] {msg}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        while True:
            try:
                item = self.qso_q.get_nowait()
            except queue.Empty:
                break
            try:
                key, call = item
                self._last_calls[key] = call
                self._apply_last(key, call)
            except Exception:
                pass
        self.root.after(150, self.poll_log)

    def _apply_last(self, key, call):
        for i in self.out_tree.get_children():
            tags = self.out_tree.item(i, "tags")
            if not tags:
                continue
            try:
                out = json.loads(tags[0])
            except ValueError:
                continue
            if self.out_key(out) == key:
                self.out_tree.set(i, "last", call)
                return

    def cfg(self):
        return {
            "inputs": [{"type": self.in_tree.item(i, "values")[0],
                        "port": int(self.in_tree.item(i, "values")[1])}
                       for i in self.in_tree.get_children()],
            "outputs": [self.out_tree.item(i, "values") and
                        json.loads(self.out_tree.item(i, "tags")[0])
                        for i in self.out_tree.get_children()],
        }

    def load_config(self):
        try:
            with open(CONFIG_FILE, encoding="utf-8") as f:
                cfg = json.load(f)
            self._cfg = cfg
        except (OSError, ValueError):
            self._cfg = {"inputs": [2237], "outputs": []}

    def save_config(self):
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(self.cfg(), f, indent=2)

    def on_save(self):
        self.save_config()
        self.messagebox.showinfo("Saved", f"Config saved to\n{CONFIG_FILE}")

    def render(self):
        self.in_tree.delete(*self.in_tree.get_children())
        for inp in self._cfg.get("inputs", []):
            if isinstance(inp, dict):
                kind, port = inp.get("type", "wsjtx"), inp.get("port", 2237)
            else:
                kind, port = "wsjtx", inp
            self.in_tree.insert("", "end", values=(kind, port))
        self.out_tree.delete(*self.out_tree.get_children())
        for out in self._cfg.get("outputs", []):
            label = self.out_label(out)
            self.out_tree.insert("", "end",
                                 values=(out.get("type"), label,
                                         self._last_calls.get(self.out_key(out), "")),
                                 tags=(json.dumps(out),))

    @staticmethod
    def out_key(out):
        t = out.get("type")
        if t == "udp":
            return "udp:%s:%s" % (out.get("host"), out.get("port"))
        if t in ("cqradio", "wavelog", "http"):
            return "http:" + (out.get("url", ""))
        if t == "adif":
            return "adif:" + (out.get("path", ""))
        return ""

    @staticmethod
    def out_label(out):
        t = out.get("type")
        if t == "udp":
            return f"{out.get('host')}:{out.get('port')}"
        if t in ("cqradio", "wavelog", "http"):
            return out.get("url", "")
        if t == "adif":
            return out.get("path", "")
        return ""

    # ---- input dialogs ----
    def add_input(self):
        import tkinter as tk
        from tkinter import ttk
        top = self.tk.Toplevel(self.root)
        top.title("Add input")
        top.resizable(False, False)
        top.transient(self.root)
        top.grab_set()
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Input type:").grid(row=0, column=0, sticky="w", pady=2)
        kind_var = tk.StringVar(value="wsjtx")
        kind_box = ttk.Combobox(
            frm, textvariable=kind_var, state="readonly",
            values=("wsjtx", "n1mm"))
        kind_box.grid(row=0, column=1, sticky="we", pady=2, padx=(6, 0))
        ttk.Label(frm, text="Listen port:").grid(row=1, column=0, sticky="w", pady=2)
        port_var = tk.StringVar(value="2237")
        ttk.Entry(frm, textvariable=port_var, width=12).grid(
            row=1, column=1, sticky="we", pady=2, padx=(6, 0))

        def on_kind(*_a):
            if kind_var.get() == "n1mm" and port_var.get() == "2237":
                port_var.set("12060")

        kind_var.trace_add("write", on_kind)
        btn = ttk.Frame(top, padding=(10, 0, 10, 10))
        btn.pack(fill="x")

        def ok():
            try:
                p = int(port_var.get().strip())
            except ValueError:
                self.messagebox.showerror("Invalid", "Port must be a number", parent=top)
                return
            self.in_tree.insert("", "end", values=(kind_var.get(), p))
            top.destroy()

        ttk.Button(btn, text="Add", command=ok).pack(side="right")
        ttk.Button(btn, text="Cancel", command=top.destroy).pack(side="right", padx=4)

        top.bind("<Return>", lambda e: ok())
        port_var_ent = None
        for w in frm.winfo_children():
            if w.winfo_class() == "TEntry":
                port_var_ent = w
        if port_var_ent is not None:
            port_var_ent.focus_set()

    def del_input(self):
        sel = self.in_tree.selection()
        if sel:
            self.in_tree.delete(sel[0])

    # ---- output dialogs ----
    def add_output(self):
        out = self.output_dialog()
        if out:
            self.out_tree.insert("", "end",
                                 values=(out.get("type"), self.out_label(out),
                                         self._last_calls.get(self.out_key(out), "")),
                                 tags=(json.dumps(out),))

    def edit_output(self):
        sel = self.out_tree.selection()
        if not sel:
            return
        current = json.loads(self.out_tree.item(sel[0], "tags")[0])
        out = self.output_dialog(current)
        if out:
            self.out_tree.item(sel[0], values=(out.get("type"), self.out_label(out),
                                               self._last_calls.get(self.out_key(out), "")),
                               tags=(json.dumps(out),))

    def del_output(self):
        sel = self.out_tree.selection()
        if sel:
            self.out_tree.delete(sel[0])

    def output_dialog(self, current=None):
        import tkinter as tk
        from tkinter import ttk
        dlg = tk.Toplevel(self.root)
        dlg.title("Output")
        dlg.grab_set()
        dlg.resizable(False, False)

        def rec():
            n = name_var.get().strip()
            t = type_var.get()
            out = {"type": t, "name": n}
            if t == "udp":
                out.update(host=host_var.get().strip(), port=int(port_var.get().strip()))
            elif t in ("cqradio", "wavelog", "http"):
                out.update(url=url_var.get().strip(), key=key_var.get().strip())
                if t == "wavelog":
                    out.update(station=station_var.get().strip())
            elif t == "adif":
                out.update(path=path_var.get().strip())
            return out

        def ok():
            try:
                o = rec()
                if o["type"] == "udp" and not (o.get("host") and 1 <= o["port"] <= 65535):
                    raise ValueError("bad host:port")
                if o["type"] in ("cqradio", "wavelog", "http") and not o.get("url"):
                    raise ValueError("URL required")
            except Exception as e:
                self.messagebox.showerror("Invalid", str(e), parent=dlg)
                return
            dlg.result = o
            dlg.destroy()

        frm = ttk.Frame(dlg, padding=10)
        frm.grid()

        defaults = {
            "name": current.get("name", "") if current else "",
            "type": current.get("type", "udp") if current else "udp",
            "host": current.get("host", "127.0.0.1") if current else "127.0.0.1",
            "port": current.get("port", 2238) if current else 2238,
            "url": (current.get("url", "") if current else
                    "https://logbook.cqradio.org/api/wsjtx/log"),
            "key": current.get("key", "") if current else "",
            "station": (str(current.get("station", "")) if current else ""),
            "path": current.get("path", "wsjtx_log.adi") if current else "wsjtx_log.adi",
        }

        ttk.Label(frm, text="Type:").grid(row=0, column=0, sticky="w")
        type_var = tk.StringVar(value=defaults["type"])
        combos = ["udp", "cqradio", "wavelog", "http", "adif"]
        type_combo = ttk.Combobox(frm, textvariable=type_var, values=combos, width=20, state="readonly")
        type_combo.grid(row=0, column=1, sticky="w", pady=3)
        type_combo.bind("<<ComboboxSelected>>", lambda e: update_form())

        ttk.Label(frm, text="Name:").grid(row=1, column=0, sticky="w")
        name_var = tk.StringVar(value=defaults["name"])
        ttk.Entry(frm, textvariable=name_var, width=36).grid(row=1, column=1, pady=3)
        name_var.trace_add("write", lambda *a: None)

        ttk.Label(frm, text="Host:").grid(row=2, column=0, sticky="w")
        host_var = tk.StringVar(value=defaults["host"])
        ttk.Entry(frm, textvariable=host_var, width=36).grid(row=2, column=1, pady=3)

        ttk.Label(frm, text="Port:").grid(row=3, column=0, sticky="w")
        port_var = tk.StringVar(value=defaults["port"])
        ttk.Entry(frm, textvariable=port_var, width=36).grid(row=3, column=1, pady=3)

        ttk.Label(frm, text="URL:").grid(row=4, column=0, sticky="w")
        url_var = tk.StringVar(value=defaults["url"])
        ttk.Entry(frm, textvariable=url_var, width=36).grid(row=4, column=1, pady=3)

        ttk.Label(frm, text="API key:").grid(row=5, column=0, sticky="w")
        key_var = tk.StringVar(value=defaults["key"])
        ttk.Entry(frm, textvariable=key_var, width=36, show="*").grid(row=5, column=1, pady=3)

        ttk.Label(frm, text="ADIF file:").grid(row=6, column=0, sticky="w")
        path_var = tk.StringVar(value=defaults["path"])
        ttk.Entry(frm, textvariable=path_var, width=36).grid(row=6, column=1, pady=3)

        ttk.Label(frm, text="Station ID:").grid(row=7, column=0, sticky="w")
        station_var = tk.StringVar(value=defaults["station"])
        ttk.Entry(frm, textvariable=station_var, width=36).grid(row=7, column=1, pady=3)

        btns = ttk.Frame(frm)
        btns.grid(row=8, column=0, columnspan=2, pady=(8, 0))
        ttk.Button(btns, text="OK", command=ok).pack(side="left", padx=4)
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="left", padx=4)

        rows = {"udp": [2, 3], "cqradio": [4, 5], "wavelog": [4, 5, 7], "http": [4, 5], "adif": [6]}

        def update_form():
            t = type_var.get()
            for row in range(2, 8):
                for w in frm.grid_slaves(row=row):
                    w.grid_remove()
            for row in rows.get(t, []):
                for w in frm.grid_slaves(row=row):
                    w.grid()
            dlg.update_idletasks()
            w = dlg.winfo_width()
            h = dlg.winfo_height()
            if w > 100 and h > 100:
                dlg.geometry(f"{w}x{h}")

        update_form()
        # keep values in trace so combobox label stays enabled; nothing
        dlg.result = None
        self.root.wait_window(dlg)
        return dlg.result

    # ---- start / stop ----
    def on_start(self):
        if self.router.running:
            self.update_buttons()
            return
        cfg = self.cfg()
        self.router.configure(cfg)
        self.router.start()
        self.update_buttons()

    def on_stop(self):
        self.router.stop()
        self.update_buttons()

    def update_buttons(self):
        if self.router.running:
            self.start_btn.configure(bg="#2e7d32", fg="white",
                                     activebackground="#1b5e20", activeforeground="white")
            self.stop_btn.configure(bg="SystemButtonFace", fg="SystemButtonText",
                                    activebackground="#d0d0d0", activeforeground="SystemButtonText")
        else:
            self.stop_btn.configure(bg="#c62828", fg="white",
                                    activebackground="#b71c1c", activeforeground="white")
            self.start_btn.configure(bg="SystemButtonFace", fg="SystemButtonText",
                                     activebackground="#d0d0d0", activeforeground="SystemButtonText")

    def on_close(self):
        try:
            self.save_config()
        except OSError:
            pass
        self.router.stop()
        self.root.destroy()


def main():
    import tkinter as tk
    root = tk.Tk()

    def app_exc_hook(exc_type, exc, tb):
        msg = "".join(traceback.format_exception(exc_type, exc, tb))
        try:
            with open(LOG_FILE, "a", encoding="utf-8", errors="replace") as f:
                f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] "
                        f"GUI ERROR\n{msg}\n")
        except OSError:
            pass

    root.report_callback_exception = app_exc_hook
    sys.excepthook = app_exc_hook
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()