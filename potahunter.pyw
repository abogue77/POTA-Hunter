#!/usr/bin/env python3
"""
POTA Hunter — POTA Activator Hunter & Ham Radio Logger  v2.0
Cross-platform (Windows & Linux).  Standard library only.

Entry form field order:
  Callsign → RST Sent → RST Received → Park # → Comments → Notes
Date/Time : stamped automatically (UTC) when LOG QSO is pressed.
Freq/Band/Mode : pulled live from Flrig on LOG QSO (falls back to VFO bar value).
Storage  : ADIF (.adi) is the primary on-disk format per logbook.
           SQLite is used as a fast in-memory working index, rebuilt on open.
"""

import tkinter as tk
from tkinter import ttk, messagebox, filedialog, simpledialog
import sqlite3
import subprocess
import xmlrpc.client
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import threading
import datetime
import time
import os
import re
import json
import tempfile
import webbrowser
import http.server
import socketserver
import sys
import wave

try:
    import sounddevice as sd
    import numpy as np
    _AUDIO_OK = True
except ImportError:
    _AUDIO_OK = False

try:
    from pynput import keyboard as _pynput_kb
    _PYNPUT_OK = True
except ImportError:
    _PYNPUT_OK = False

# ── Paths ─────────────────────────────────────────────────────────────────────
LOGBOOK_DIR = os.path.join(os.path.expanduser("~"), "POTA-Hunter")
os.makedirs(LOGBOOK_DIR, exist_ok=True)
CONFIG_FILE   = os.path.join(LOGBOOK_DIR, "config.json")
PARKS_DB      = os.path.join(LOGBOOK_DIR, "pota_parks.db")
FCC_DB        = os.path.join(LOGBOOK_DIR, "fcc_calls.db")
PARKS_CSV_URL = "https://pota.app/all_parks_ext.csv"
FCC_ZIP_URLS  = [
    "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip",   # official
    "http://www.nc7j.com/downloads/AR6/fcc/complete/l_amat.zip",   # community mirror
]

# ── Config ────────────────────────────────────────────────────────────────────
DEFAULT_CONFIG = {
    "callsign":   "",
    "gridsquare": "",
    "qrz_user":   "",
    "qrz_pass":   "",
    "flrig_host": "127.0.0.1",
    "flrig_port": 12345,
    "last_logbook": "",
    "theme":      "dark",
    "pota_band":              "All",
    "pota_mode":              "All",
    "pota_hide_qrt":          False,
    "pota_hide_oob":          False,
    "pota_itu_r1":            True,
    "pota_itu_r2":            True,
    "pota_itu_r3":            True,
    "pota_respot_enabled":    False,
    "pota_scan_skip_worked":  False,
    "pota_scan_interval":     15,
    "fcc_db_date":            "",
    "license_class":          "Extra",
    "flrig_autostart":        False,
    "flrig_exe_path":         "",
    "vk_labels": {
        "F1": "CQ CQ CQ", "F2": "My Call", "F3": "Report",
        "F4": "Name / QTH", "F5": "73", "F6": "", "F7": "", "F8": "", "F9": "",
    },
    "vk_ptt_enabled":     False,
    "vk_ptt_delay_ms":    200,
    "vk_input_device":    "",
    "vk_output_device":   "",
}

def load_config():
    if os.path.exists(CONFIG_FILE):
        try:
            with open(CONFIG_FILE) as f:
                cfg = DEFAULT_CONFIG.copy()
                cfg.update(json.load(f))
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ── SQLite in-memory index ────────────────────────────────────────────────────
def make_index():
    conn = sqlite3.connect(":memory:", check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE qso (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            call       TEXT NOT NULL,
            date       TEXT NOT NULL,
            time_on    TEXT NOT NULL,
            freq       REAL,
            band       TEXT,
            mode       TEXT,
            rst_sent   TEXT DEFAULT '59',
            rst_rcvd   TEXT DEFAULT '59',
            name       TEXT,
            qth        TEXT,
            gridsquare TEXT,
            park_nr    TEXT,
            comment    TEXT,
            notes      TEXT
        )
    """)
    conn.commit()
    return conn

# ── POTA Parks DB ─────────────────────────────────────────────────────────────
def _latlon_to_grid(lat, lon):
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    lon += 180.0
    lat += 90.0
    field_lon = int(lon / 20)
    field_lat = int(lat / 10)
    sq_lon    = int((lon % 20) / 2)
    sq_lat    = int(lat % 10)
    return (chr(ord('A') + field_lon) +
            chr(ord('A') + field_lat) +
            str(sq_lon) +
            str(sq_lat))

def parks_db_exists():
    if not os.path.exists(PARKS_DB):
        return False
    try:
        with sqlite3.connect(PARKS_DB) as cx:
            n = cx.execute("SELECT COUNT(*) FROM parks").fetchone()[0]
        return n > 0
    except Exception:
        return False

def build_parks_db(progress_cb=None):
    """Download all_parks_ext.csv and rebuild ~/POTA-Hunter/pota_parks.db.
    Returns (count, error_string). error_string is None on success.
    Designed to run in a background thread; progress_cb(msg) is thread-safe."""
    import csv, io
    if progress_cb:
        progress_cb("Downloading POTA parks list…")
    try:
        req = urllib.request.Request(
            PARKS_CSV_URL,
            headers={"User-Agent": "POTA-Hunter/2.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, f"Download failed: {e}"

    if progress_cb:
        progress_cb("Parsing CSV…")

    reader = csv.DictReader(io.StringIO(raw))
    FIELD_MAP = {
        "reference": ["reference", "park_reference", "park reference"],
        "name":      ["name", "park_name", "park name"],
        "latitude":  ["latitude", "lat"],
        "longitude": ["longitude", "lon", "long"],
        "grid":      ["grid", "gridsquare", "grid_square"],
        "state":     ["locationname", "state", "location_name"],
        "country":   ["entityname", "country", "entity_name"],
    }

    rows = []
    for rec in reader:
        lc = {k.strip().lower(): v.strip() for k, v in rec.items()}

        def pick(candidates):
            for c in candidates:
                if lc.get(c):
                    return lc[c]
            return ""

        ref = pick(FIELD_MAP["reference"]).upper()
        if not ref:
            continue
        lat  = pick(FIELD_MAP["latitude"])
        lon  = pick(FIELD_MAP["longitude"])
        grid = pick(FIELD_MAP["grid"])
        if not grid:
            grid = _latlon_to_grid(lat, lon)
        rows.append((
            ref,
            pick(FIELD_MAP["name"]),
            lat,
            lon,
            grid.upper()[:6],
            pick(FIELD_MAP["state"]),
            pick(FIELD_MAP["country"]),
        ))

    if not rows:
        return 0, "CSV parsed but contained no usable park records."

    if progress_cb:
        progress_cb(f"Writing {len(rows):,} parks to DB…")

    try:
        with sqlite3.connect(PARKS_DB) as cx:
            cx.execute("DROP TABLE IF EXISTS parks")
            cx.execute("""
                CREATE TABLE parks (
                    reference TEXT PRIMARY KEY,
                    name      TEXT,
                    latitude  REAL,
                    longitude REAL,
                    grid      TEXT,
                    state     TEXT,
                    country   TEXT
                )
            """)
            cx.execute("CREATE INDEX IF NOT EXISTS idx_parks_ref ON parks(reference)")
            cx.executemany("INSERT OR REPLACE INTO parks VALUES (?,?,?,?,?,?,?)", rows)
    except Exception as e:
        return 0, f"DB write failed: {e}"

    if progress_cb:
        progress_cb(f"POTA parks DB ready — {len(rows):,} parks.")
    return len(rows), None

def lookup_park(reference):
    """Return a dict with park data or None. Thread-safe (new connection per call)."""
    if not os.path.exists(PARKS_DB):
        return None
    try:
        with sqlite3.connect(PARKS_DB) as cx:
            cx.row_factory = sqlite3.Row
            row = cx.execute(
                "SELECT * FROM parks WHERE reference=?",
                (reference.strip().upper(),)).fetchone()
        return dict(row) if row else None
    except Exception:
        return None

# ── FCC Callsign DB ───────────────────────────────────────────────────────────
def fcc_db_exists():
    if not os.path.exists(FCC_DB):
        return False
    try:
        with sqlite3.connect(FCC_DB) as cx:
            n = cx.execute("SELECT COUNT(*) FROM callsigns").fetchone()[0]
        return n > 0
    except Exception:
        return False

def build_fcc_db(progress_cb=None):
    """Download l_amat.zip from FCC and build ~/POTA-Hunter/fcc_calls.db.
    Returns (count, error_string). Runs in a background thread; progress_cb is thread-safe."""
    import zipfile, io as _io
    if progress_cb:
        progress_cb("Connecting to FCC database server…")
    raw = None
    last_err = ""
    for url in FCC_ZIP_URLS:
        source = "FCC" if "fcc.gov" in url else "mirror"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "POTA-Hunter/2.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                chunks = []
                received = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    received += len(chunk)
                    if progress_cb:
                        if total:
                            pct = received * 100 // total
                            progress_cb(
                                f"Downloading FCC database ({source})…  "
                                f"{received/1_048_576:.1f} / {total/1_048_576:.1f} MB  ({pct}%)")
                        else:
                            progress_cb(
                                f"Downloading FCC database ({source})…  "
                                f"{received/1_048_576:.1f} MB received")
            raw = b"".join(chunks)
            break  # success
        except Exception as e:
            last_err = str(e)
            if progress_cb:
                progress_cb(f"{source} unavailable ({e}) — trying next source…")
    if raw is None:
        return 0, f"Download failed from all sources: {last_err}"

    if progress_cb:
        progress_cb("Parsing FCC data files…")
    try:
        zf = zipfile.ZipFile(_io.BytesIO(raw))

        # HD.dat: pipe-delimited, field[4]=callsign, field[5]=status (A=Active)
        active = set()
        with zf.open("HD.dat") as f:
            for line in f:
                parts = line.decode("latin-1").rstrip("\r\n").split("|")
                if len(parts) > 5 and parts[5] == "A":
                    active.add(parts[4].upper())

        # AM.dat: pipe-delimited, field[4]=callsign, field[5]=operator class (E/G/A/T/N/P)
        classes = {}
        with zf.open("AM.dat") as f:
            for line in f:
                parts = line.decode("latin-1").rstrip("\r\n").split("|")
                if len(parts) > 5 and parts[4]:
                    classes[parts[4].upper()] = parts[5]

        # EN.dat: pipe-delimited, field[4]=callsign, [7]=entity name (clubs),
        #         [8]=first, [10]=last, [15]=addr, [16]=city, [17]=state, [18]=zip
        rows = []
        with zf.open("EN.dat") as f:
            for line in f:
                parts = line.decode("latin-1").rstrip("\r\n").split("|")
                if len(parts) < 19:
                    continue
                call = parts[4].upper()
                if not call or call not in active:
                    continue
                first  = parts[8].strip()
                last   = parts[10].strip()
                entity = parts[7].strip()
                if not first and not last and entity:
                    first = entity
                rows.append((
                    call,
                    first,
                    last,
                    parts[15].strip(),
                    parts[16].strip(),
                    parts[17].strip(),
                    parts[18].strip(),
                    classes.get(call, ""),
                ))
    except Exception as e:
        return 0, f"Parse failed: {e}"

    if not rows:
        return 0, "No active licensee records found in FCC data."

    if progress_cb:
        progress_cb(f"Writing {len(rows):,} callsigns to DB…")
    try:
        with sqlite3.connect(FCC_DB) as cx:
            cx.execute("DROP TABLE IF EXISTS callsigns")
            cx.execute("""
                CREATE TABLE callsigns (
                    call       TEXT PRIMARY KEY,
                    first_name TEXT,
                    last_name  TEXT,
                    addr1      TEXT,
                    city       TEXT,
                    state      TEXT,
                    zip        TEXT,
                    lic_class  TEXT
                )
            """)
            cx.execute("CREATE INDEX IF NOT EXISTS idx_fcc_call ON callsigns(call)")
            cx.executemany("INSERT OR REPLACE INTO callsigns VALUES (?,?,?,?,?,?,?,?)", rows)
    except Exception as e:
        return 0, f"DB write failed: {e}"

    if progress_cb:
        progress_cb(f"FCC database ready — {len(rows):,} callsigns.")
    return len(rows), None

def fcc_lookup(call):
    """Look up a callsign in the local FCC database. Thread-safe."""
    if not os.path.exists(FCC_DB):
        return None
    try:
        with sqlite3.connect(FCC_DB) as cx:
            row = cx.execute(
                "SELECT first_name,last_name,addr1,city,state,lic_class "
                "FROM callsigns WHERE call=?",
                (call.upper(),)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    fn, ln, addr, city, state, cls = row
    name = f"{fn} {ln}".strip()
    qth  = f"{city}, {state}".strip(", ")
    return {"name": name, "qth": qth, "class": cls, "source": "FCC"}

# ── ADIF helpers ──────────────────────────────────────────────────────────────
def adif_field(tag, val):
    if val is None or str(val).strip() == "":
        return ""
    v = str(val).strip()
    return f"<{tag.upper()}:{len(v)}>{v} "

def adif_header(mycall):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    prog = "POTA Hunter"
    return (f"POTA Hunter ADIF Log — {now}  Station: {mycall}\n"
            + adif_field("ADIF_VER","3.1.0")
            + adif_field("PROGRAMID", prog)
            + "<EOH>\n\n")

def row_to_adif(row, mycall=""):
    r = dict(row)
    date_str = r.get("date","").replace("-","")
    freq_val = r.get("freq")
    freq_str = f"{float(freq_val):.6g}" if freq_val else ""
    parts = (
        adif_field("CALL",             r.get("call","")) +
        adif_field("QSO_DATE",         date_str) +
        adif_field("TIME_ON",          r.get("time_on","")) +
        adif_field("FREQ",             freq_str) +
        adif_field("BAND",             r.get("band","")) +
        adif_field("MODE",             r.get("mode","")) +
        adif_field("RST_SENT",         r.get("rst_sent","")) +
        adif_field("RST_RCVD",         r.get("rst_rcvd","")) +
        adif_field("NAME",             r.get("name","")) +
        adif_field("QTH",              r.get("qth","")) +
        adif_field("GRIDSQUARE",       r.get("gridsquare","")) +
        adif_field("POTA_REF",         r.get("park_nr","")) +
        adif_field("COMMENT",          r.get("comment","")) +
        adif_field("NOTES",            r.get("notes","")) +
        adif_field("STATION_CALLSIGN", mycall)
    )
    return parts.strip() + " <EOR>\n"

def parse_adif_records(text):
    eoh = text.upper().find("<EOH>")
    if eoh >= 0:
        text = text[eoh + 5:]
    records = []
    for rec in re.split(r'<EOR>', text, flags=re.IGNORECASE):
        rec = rec.strip()
        if not rec:
            continue
        d = {}
        for m in re.finditer(r'<(\w+):\d+(?::\w+)?>([^<]*)', rec, re.DOTALL):
            d[m.group(1).upper()] = m.group(2).strip()
        if d.get("CALL","").strip():
            records.append(d)
    return records

def adif_to_row_dict(d):
    qso_date = d.get("QSO_DATE","")
    if len(qso_date) == 8:
        qso_date = f"{qso_date[:4]}-{qso_date[4:6]}-{qso_date[6:]}"
    freq_str = d.get("FREQ","")
    try:
        freq = float(freq_str) if freq_str else None
    except ValueError:
        freq = None
    band = d.get("BAND","") or (freq_to_band(freq) if freq else "")
    return {
        "call":       d.get("CALL","").upper(),
        "date":       qso_date,
        "time_on":    d.get("TIME_ON",""),
        "freq":       freq,
        "band":       band,
        "mode":       d.get("MODE",""),
        "rst_sent":   d.get("RST_SENT","59"),
        "rst_rcvd":   d.get("RST_RCVD","59"),
        "name":       d.get("NAME",""),
        "qth":        d.get("QTH",""),
        "gridsquare": d.get("GRIDSQUARE","") or d.get("GRID",""),
        "park_nr":    d.get("POTA_REF",""),
        "comment":    d.get("COMMENT",""),
        "notes":      d.get("NOTES",""),
    }

def load_adif_into_index(adif_path, conn):
    conn.execute("DELETE FROM qso")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='qso'")
    conn.commit()
    if not os.path.exists(adif_path):
        return 0
    with open(adif_path, "r", encoding="utf-8", errors="replace") as f:
        text = f.read()
    records = parse_adif_records(text)
    for rec in records:
        row = adif_to_row_dict(rec)
        conn.execute("""
            INSERT INTO qso (call,date,time_on,freq,band,mode,
                rst_sent,rst_rcvd,name,qth,gridsquare,park_nr,comment,notes)
            VALUES (:call,:date,:time_on,:freq,:band,:mode,
                :rst_sent,:rst_rcvd,:name,:qth,:gridsquare,:park_nr,:comment,:notes)
        """, row)
    conn.commit()
    return len(records)

def rewrite_adif(adif_path, conn, mycall=""):
    rows = conn.execute(
        "SELECT * FROM qso ORDER BY date ASC, time_on ASC").fetchall()
    with open(adif_path, "w", encoding="utf-8") as f:
        f.write(adif_header(mycall))
        for row in rows:
            f.write(row_to_adif(row, mycall))

# ── Frequency ─────────────────────────────────────────────────────────────────
def freq_to_band(freq_mhz):
    try:
        f = float(freq_mhz)
    except (TypeError, ValueError):
        return ""
    bands = [
        (1.8,2.0,"160m"),(3.5,4.0,"80m"),(5.3,5.4,"60m"),
        (7.0,7.3,"40m"),(10.1,10.15,"30m"),(14.0,14.35,"20m"),
        (18.068,18.168,"17m"),(21.0,21.45,"15m"),(24.89,24.99,"12m"),
        (28.0,29.7,"10m"),(50.0,54.0,"6m"),(144.0,148.0,"2m"),
        (420.0,450.0,"70cm"),
    ]
    for lo, hi, band in bands:
        if lo <= f <= hi:
            return band
    return ""

# ── US Amateur Band Plan (FCC Part 97, MHz) ───────────────────────────────────
# Phone/SSB/AM/FM sub-band privileges by license class
_US_PHONE_BANDS = {
    "Extra":      [(1.800,2.000),(3.600,4.000),(5.3305,5.4035),(7.125,7.300),
                   (14.150,14.350),(18.110,18.168),(21.200,21.450),(24.930,24.990),
                   (28.000,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
    "General":    [(1.843,2.000),(3.800,4.000),(5.3305,5.4035),(7.175,7.300),
                   (14.225,14.350),(18.110,18.168),(21.275,21.450),(24.930,24.990),
                   (28.300,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
    "Technician": [(28.300,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
}
# CW/digital sub-band privileges (broader than phone)
_US_CW_BANDS = {
    "Extra":      [(1.800,2.000),(3.500,4.000),(5.3305,5.4035),(7.000,7.300),
                   (10.100,10.150),(14.000,14.350),(18.068,18.168),(21.000,21.450),
                   (24.890,24.990),(28.000,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
    "General":    [(1.800,2.000),(3.525,4.000),(5.3305,5.4035),(7.025,7.300),
                   (10.100,10.150),(14.025,14.350),(18.068,18.168),(21.025,21.450),
                   (24.890,24.990),(28.000,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
    "Technician": [(3.525,3.600),(7.025,7.125),(21.025,21.200),(28.000,29.700),
                   (50.0,54.0),(144.0,148.0),(420.0,450.0)],
}
_PHONE_MODES = {"SSB","USB","LSB","AM","FM","FMN","PHONE"}

def _in_band(freq_mhz, license_class, mode=""):
    plan = _US_PHONE_BANDS if mode.strip().upper() in _PHONE_MODES else _US_CW_BANDS
    ranges = plan.get(license_class)
    if not ranges:
        return True
    return any(lo <= freq_mhz <= hi for lo, hi in ranges)

# ── Flrig ─────────────────────────────────────────────────────────────────────
class _TimeoutTransport(xmlrpc.client.Transport):
    """XML-RPC transport with a configurable connection/read timeout."""
    def __init__(self, timeout=2.0):
        super().__init__()
        self._timeout = timeout

    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn

def flrig_get(host, port):
    try:
        proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}/RPC2",
            transport=_TimeoutTransport(timeout=2.0),
            allow_none=True)
        return proxy.rig.get_vfo(), proxy.rig.get_mode()
    except Exception:
        return None, None

def flrig_get_all(host, port):
    """Single-session fetch: freq, mode, s-meter, power meter, PTT state."""
    try:
        proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}/RPC2",
            transport=_TimeoutTransport(timeout=2.0),
            allow_none=True)
        freq_hz  = proxy.rig.get_vfo()
        mode     = proxy.rig.get_mode()
        smeter   = proxy.rig.get_smeter()
        pwrmeter = proxy.rig.get_pwrmeter()
        try:
            ptt = bool(proxy.rig.get_ptt())
        except Exception:
            ptt = pwrmeter is not None and float(pwrmeter) > 5
        return freq_hz, mode, smeter, pwrmeter, ptt
    except Exception:
        return None, None, None, None, False

def flrig_set_freq(host, port, freq_hz):
    """Returns True on success or an error string on failure."""
    try:
        proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}/RPC2",
            transport=_TimeoutTransport(timeout=5.0),
            allow_none=True)
        proxy.rig.set_vfo(float(freq_hz))
        return True
    except Exception as e:
        return str(e)

def ssb_mode_for_freq(freq_hz):
    """Return 'LSB' for HF below 10 MHz, 'USB' at 10 MHz and above.
    60m (5.3305–5.4035 MHz) is USB by FCC regulation despite being below 10 MHz."""
    mhz = float(freq_hz) / 1_000_000
    if 5.3305 <= mhz <= 5.4035:
        return "USB"
    return "USB" if mhz >= 10.0 else "LSB"

def flrig_set_freq_and_mode(host, port, freq_hz, spot_mode=""):
    """Set VFO frequency and, for SSB spots, auto-select LSB/USB.
    Returns True on success or an error string on failure."""
    try:
        proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}/RPC2",
            transport=_TimeoutTransport(timeout=5.0),
            allow_none=True)
        proxy.rig.set_vfo(float(freq_hz))
        norm = spot_mode.strip().upper()
        if norm in ("SSB", "USB", "LSB", ""):
            mode = ssb_mode_for_freq(freq_hz)
            proxy.rig.set_mode(mode)
        return True
    except Exception as e:
        return str(e)

def flrig_set_ptt(host, port, state: bool) -> bool:
    """Assert (state=True) or release (state=False) PTT via Flrig XML-RPC."""
    try:
        proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}/RPC2",
            transport=_TimeoutTransport(timeout=2.0),
            allow_none=True)
        proxy.rig.set_ptt(1 if state else 0)
        return True
    except Exception:
        return False

# ── POTA spot posting ─────────────────────────────────────────────────────────
def pota_post_spot(activator, spotter, reference, freq_khz, mode, comment=""):
    import json as _json
    body = _json.dumps({
        "activator":  activator,
        "spotter":    spotter,
        "reference":  reference,
        "frequency":  str(freq_khz),
        "mode":       mode,
        "comments":   comment or "Spotted via POTA Hunter",
    }).encode()
    req = urllib.request.Request(
        "https://api.pota.app/spot",
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "User-Agent":   "POTA-Hunter/2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)

# ── QRZ ───────────────────────────────────────────────────────────────────────
_qrz_session = None

def qrz_login(user, password):
    global _qrz_session
    url = (f"https://xmldata.qrz.com/xml/current/?username={urllib.parse.quote(user)}"
           f"&password={urllib.parse.quote(password)}&agent=POTA-Hunter2.0")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            tree = ET.parse(r)
        key = tree.find(".//{http://xmldata.qrz.com}Key")
        if key is not None:
            _qrz_session = key.text
            return True
        err = tree.find(".//{http://xmldata.qrz.com}Error")
        return err.text if err is not None else "Login failed"
    except Exception as e:
        return str(e)

def qrz_lookup(call):
    if not _qrz_session:
        return None
    url = (f"https://xmldata.qrz.com/xml/current/?s={_qrz_session}"
           f"&callsign={urllib.parse.quote(call.upper())}")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            tree = ET.parse(r)
        ns = "{http://xmldata.qrz.com}"
        def g(tag):
            el = tree.find(f".//{ns}{tag}")
            return el.text if el is not None else ""
        return {"name": f"{g('fname')} {g('name')}".strip(),
                "qth":  f"{g('addr2')}, {g('country')}".strip(", "),
                "grid": g("grid")}
    except Exception:
        return None

def hamdb_lookup(call):
    """Look up a callsign via the free HamDB.org API. No auth required."""
    url = f"http://api.hamdb.org/{urllib.parse.quote(call.upper())}/json/POTA-Hunter"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "POTA-Hunter/2.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())["hamdb"]["callsign"]
        if data.get("call", "").upper() == "NOT_FOUND":
            return None
        fname = data.get("fname", "")
        lname = data.get("name", "")
        name  = f"{fname} {lname}".strip()
        city  = data.get("addr2", "")
        state = data.get("state", "")
        qth   = f"{city}, {state}".strip(", ")
        return {
            "name":   name,
            "qth":    qth,
            "grid":   data.get("grid", ""),
            "class":  data.get("class", ""),
            "source": "HamDB",
        }
    except Exception:
        return None

# ── Palettes ──────────────────────────────────────────────────────────────────
DARK_PALETTE = {
    "BG": "#111318", "BG2": "#1a1d24", "BG3": "#22262f", "BG4": "#2a2f3a",
    "ACCENT": "#e8a020", "ACC2": "#4fc3f7", "ACC3": "#81c995",
    "WARN": "#f28b82", "YELLOW": "#d4c020", "MUTED": "#555e6e", "FG": "#dde3ee", "FG2": "#8c95a6",
    "SEL": "#2d3a52",
    "MAP_BG": "#0a0e17", "MAP_LAND": "#12201a", "MAP_GRID": "#151c28", "MAP_GRID2": "#2a3347",
    "MAP_COAST": "#1e3a5f", "MAP_GLOW": "#5a3010",
    "POTA_TUNED": "#0077ff", "POTA_WORKED": "#00cc44",
}
LIGHT_PALETTE = {
    "BG": "#f5f7fa", "BG2": "#eaecf2", "BG3": "#dde1ea", "BG4": "#ced3df",
    "ACCENT": "#b07800", "ACC2": "#0066aa", "ACC3": "#2a7a30",
    "WARN": "#cc2222", "YELLOW": "#b8a000", "MUTED": "#7a8599", "FG": "#1a1d24", "FG2": "#4a5568",
    "SEL": "#b3c9e8",
    "MAP_BG": "#d0dce8", "MAP_LAND": "#c8d8c0", "MAP_GRID": "#b0c4d8", "MAP_GRID2": "#8aaac8",
    "MAP_COAST": "#4a7ab0", "MAP_GLOW": "#d4a040",
    "POTA_TUNED": "#5588ff", "POTA_WORKED": "#00cc55",
}

def _apply_palette(name="dark"):
    g = globals()
    for k, v in (LIGHT_PALETTE if name == "light" else DARK_PALETTE).items():
        g[k] = v

_apply_palette("dark")  # defaults; entry point overrides with saved preference

MONO  = ("Courier New", 10)
DISP  = ("Courier New", 22, "bold")
SM    = ("Courier New", 9)
LBL   = ("Courier New", 10, "bold")
TITLE = ("Courier New", 13, "bold")

MODES = ["USB","LSB","SSB","AM","FM","FMN","CW","CWR","RTTY","RTTYR",
         "PKTUSB","PKTLSB","PKTFM","JS8","FT8","FT4","PSK31","OLIVIA","HELL"]
BANDS = ["160m","80m","60m","40m","30m","20m","17m","15m","12m","10m",
         "6m","2m","70cm","SAT","Other"]

def _make_reticle_img(size, fg_hex, bg_hex):
    """Return a tk.PhotoImage of a scope reticle (crosshair + circle), encoded as PNG."""
    import math, struct, zlib, base64
    def _rgb(h):
        h = h.lstrip('#')
        return (int(h[0:2],16), int(h[2:4],16), int(h[4:6],16))
    fg, bg = _rgb(fg_hex), _rgb(bg_hex)
    buf = bytearray()
    for _ in range(size * size):
        buf.extend(bg)
    def put(x, y):
        if 0 <= x < size and 0 <= y < size:
            i = (y * size + x) * 3
            buf[i:i+3] = bytes(fg)
    cx = cy = size // 2
    gap = 3
    radius = size // 2 - 2
    for x in range(size):
        if abs(x - cx) > gap:
            for dy in (-1, 0, 1):
                put(x, cy + dy)
    for y in range(size):
        if abs(y - cy) > gap:
            for dx in (-1, 0, 1):
                put(cx + dx, y)
    for a in range(720):
        rad = math.radians(a / 2)
        put(int(round(cx + radius * math.cos(rad))),
            int(round(cy + radius * math.sin(rad))))
    def _png_chunk(tag, data):
        c = tag + data
        return struct.pack('>I', len(data)) + c + struct.pack('>I', zlib.crc32(c) & 0xffffffff)
    ihdr = struct.pack('>IIBBBBB', size, size, 8, 2, 0, 0, 0)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        raw.extend(buf[y * size * 3:(y + 1) * size * 3])
    png = (b'\x89PNG\r\n\x1a\n'
           + _png_chunk(b'IHDR', ihdr)
           + _png_chunk(b'IDAT', zlib.compress(bytes(raw)))
           + _png_chunk(b'IEND', b''))
    return tk.PhotoImage(data=base64.b64encode(png).decode())

# ── Simplified world coastline outlines (lon, lat) ────────────────────────────
# Natural Earth 110m land polygons — embedded compressed data
import zlib as _zlib, base64 as _b64, json as _json
_NE110_B64 = """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
bl4GI+GlOZEmop7t+fDjhr/fP+AVsnc="""
NE110_LAND = _json.loads(_zlib.decompress(_b64.b64decode(
    _NE110_B64.replace(chr(10), ""))))


# ── Maidenhead grid decoder ───────────────────────────────────────────────────
def grid_to_latlon(gs):
    """Return (lat, lon) center of a 4- or 6-char Maidenhead grid square."""
    gs = (gs or "").strip().upper()
    if len(gs) < 4:
        return None, None
    try:
        lon = (ord(gs[0]) - ord('A')) * 20 - 180 + int(gs[2]) * 2
        lat = (ord(gs[1]) - ord('A')) * 10 - 90  + int(gs[3]) * 1
        if len(gs) >= 6:
            lon += (ord(gs[4].lower()) - ord('a')) * (5 / 60) + (2.5 / 60)
            lat += (ord(gs[5].lower()) - ord('a')) * (2.5 / 60) + (1.25 / 60)
        else:
            lon += 1.0
            lat += 0.5
        return lat, lon
    except (ValueError, IndexError):
        return None, None

def lat_lon_to_itu_region(lat, lon):
    if lon <= -30:
        return 2
    elif lon <= 60:
        return 1
    else:
        return 3

# ══════════════════════════════════════════════════════════════════════════════
def _resource(relative):
    """Resolve path to a bundled resource (works both frozen and from source)."""
    if hasattr(sys, "_MEIPASS"):
        return os.path.join(sys._MEIPASS, relative)
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), relative)


class POTAHunter(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("POTA Hunter by n5eab")
        self.configure(bg=BG)
        self.minsize(1000, 640)
        self.resizable(True, True)
        try:
            self.iconbitmap(_resource(os.path.join("assets", "icon.ico")))
        except Exception:
            pass

        self.cfg           = load_config()
        self.conn          = make_index()
        self.adif_path     = ""
        self._map_poll_id    = None
        self._map_adif_mtime = None
        self._flrig_freq_hz  = None
        self._flrig_mode     = None
        self._flrig_poll_id  = None
        self._meter_value    = 0
        self._meter_is_tx    = False
        self._tune_suppress_until = 0.0
        self._pota_paused    = False
        self._pota_loaded    = False
        self._pota_after_id  = None
        self._pota_spots_raw      = []
        self._pota_spots_filtered = []
        self._freq_check_var    = tk.StringVar()
        self._freq_check_border = None
        self._freq_check_label  = None
        self._pota_band_var  = tk.StringVar(value=self.cfg.get("pota_band", "All"))
        self._pota_mode_var  = tk.StringVar(value=self.cfg.get("pota_mode", "All"))
        self._pota_hide_qrt  = tk.BooleanVar(value=self.cfg.get("pota_hide_qrt", False))
        self._pota_hide_oob  = tk.BooleanVar(value=self.cfg.get("pota_hide_oob", False))
        self._pota_itu_r1    = tk.BooleanVar(value=self.cfg.get("pota_itu_r1", True))
        self._pota_itu_r2    = tk.BooleanVar(value=self.cfg.get("pota_itu_r2", True))
        self._pota_itu_r3    = tk.BooleanVar(value=self.cfg.get("pota_itu_r3", True))
        self._pota_clicked_hz    = None
        self._pota_scan_active       = False
        self._pota_scan_idx          = 0
        self._pota_scan_after_id     = None
        self._scan_paused_by_map     = False
        self._flrig_ptt              = False
        self._pota_scan_interval     = tk.IntVar(value=self.cfg.get("pota_scan_interval", 15))
        self._pota_scan_skip_worked  = tk.BooleanVar(value=self.cfg.get("pota_scan_skip_worked", False))
        self._pota_spot_ctx          = None
        self._last_tuned_spot_key    = None   # (activator, park) of most-recently tuned spot
        self._pota_respot_enabled    = tk.BooleanVar(value=self.cfg.get("pota_respot_enabled", False))
        self._map_server             = None
        self._map_server_port        = None
        self._map_markers       = {}
        self._map_drawn         = False
        self._map_resize_id     = None
        self._map_show_spots    = tk.BooleanVar(value=True)
        self._map_zoom          = 1.0
        self._map_pan_x         = 0.0
        self._map_pan_y         = 0.0
        self._map_drag_start    = None
        self._map_click_origin  = None
        self._map_spot_data     = {}
        self._map_flash_state   = False
        self._map_flash_id      = None
        self._map_flash_grids   = set()
        self._map_spot_flash_state = False
        self._map_spot_flash_id    = None
        self._map_spot_flash_grids = set()
        self._map_marker_items  = {}
        self._map_scan_blink_st = False
        self._map_scan_blink_id = None
        self._map_beam_id       = None
        self._map_beam_phase    = 0
        self._map_my_px         = None
        self._map_tuned_pxs     = []

        self._vk_dir      = os.path.join(LOGBOOK_DIR, "voice_keyer")
        os.makedirs(self._vk_dir, exist_ok=True)
        self._vk_window   = None
        self._vk_listener = None

        self._style_ttk()
        self._build_menu()
        self._build_ui()

        last = self.cfg.get("last_logbook","")
        if last and os.path.exists(last):
            self._open_adif(last)
        else:
            self.after(200, self._prompt_logbook)

        if self.cfg["qrz_user"] and self.cfg["qrz_pass"]:
            threading.Thread(target=self._qrz_login_bg, daemon=True).start()

        self._flrig_proc = None
        self._flrig_proc_owned = False
        self._start_flrig_poll()
        self._maybe_start_flrig()
        self.after(1500, self._check_parks_db_on_startup)
        self.after(2000, self._check_fcc_db_on_startup)
        self._start_vk_hotkey_listener()

    # ── TTK style ─────────────────────────────────────────────────────────
    def _style_ttk(self):
        s = ttk.Style()
        s.theme_use("clam")
        s.configure("Treeview", background=BG2, foreground=FG,
                    fieldbackground=BG2, rowheight=22, font=MONO)
        s.configure("Treeview.Heading", background=BG3, foreground=ACCENT,
                    font=LBL, relief="flat")
        # The clam theme (and Windows visual styles) inject a
        # ("!disabled", "!selected") map entry that overrides tag backgrounds
        # for all normal rows.  Strip it so tag_configure colours show through.
        def _strip(opt):
            return [e for e in s.map("Treeview", query_opt=opt)
                    if e[:2] != ("!disabled", "!selected")]
        s.map("Treeview",
              background=_strip("background") + [("selected", SEL)],
              foreground=_strip("foreground") + [("selected", ACC2)])
        s.configure("TCombobox", fieldbackground=BG3, background=BG3,
                    foreground=FG, arrowcolor=ACCENT)
        s.map("TCombobox", fieldbackground=[("readonly",BG3)],
                           foreground=[("readonly",FG)])
        s.configure("TNotebook", background=BG, borderwidth=0, tabmargins=0)
        s.configure("TNotebook.Tab", background=BG3, foreground=FG2,
                    font=LBL, padding=[14, 5])
        s.map("TNotebook.Tab",
              background=[("selected", BG2)],
              foreground=[("selected", ACCENT)])

    # ── Menu ──────────────────────────────────────────────────────────────
    def _build_menu(self):
        mb = tk.Menu(self, bg=BG2, fg=FG, activebackground=BG4,
                     activeforeground=ACCENT, relief="flat", bd=0)
        self.config(menu=mb)
        def menu(label):
            m = tk.Menu(mb, tearoff=0, bg=BG2, fg=FG,
                        activebackground=BG4, activeforeground=ACCENT)
            mb.add_cascade(label=label, menu=m)
            return m
        fm = menu("File")
        fm.add_command(label="New Logbook…",  command=self._new_logbook)
        fm.add_command(label="Open Logbook…", command=self._choose_logbook)
        fm.add_separator()
        fm.add_command(label="Import ADIF…",  command=self._import_adif)
        fm.add_command(label="Export ADIF…",  command=self._export_adif)
        fm.add_separator()
        fm.add_command(label="Exit",          command=self.destroy)
        sm = menu("Settings")
        sm.add_command(label="Station Settings…", command=self._station_settings)
        sm.add_command(label="QRZ Login…",        command=self._qrz_settings)
        sm.add_command(label="Flrig Settings…",   command=self._flrig_settings)
        sm.add_separator()
        sm.add_command(label="Save Filter Settings", command=self._save_filter_settings)
        sm.add_separator()
        sm.add_command(label="Update POTA Parks DB…", command=self._update_parks_db)
        fcc_date = self.cfg.get("fcc_db_date", "")
        fcc_label = (f"Update FCC Callsign DB… (last: {fcc_date})"
                     if fcc_date else "Download FCC Callsign DB…")
        sm.add_command(label=fcc_label, command=self._update_fcc_db)
        sm.add_separator()
        theme_label = ("☀ Switch to Light Mode"
                       if self.cfg.get("theme", "dark") == "dark"
                       else "☾ Switch to Dark Mode")
        sm.add_command(label=theme_label, command=self._switch_theme)
        tm = menu("Tools")
        tm.add_command(label="Voice Keyer…", command=self._open_voice_keyer)
        hm = menu("Help")
        hm.add_command(label="About", command=self._about)

    # ── Main UI ───────────────────────────────────────────────────────────
    def _build_ui(self):
        # Top bar
        top = tk.Frame(self, bg=BG, pady=5)
        top.pack(fill="x", padx=14)
        tk.Label(top, text="◈ POTA Hunter by n5eab", bg=BG, fg=ACCENT, font=TITLE).pack(side="left")
        self._logbook_lbl = tk.Label(top, text="No logbook open", bg=BG, fg=FG2, font=SM)
        self._logbook_lbl.pack(side="left", padx=14)
        self._flrig_lbl = tk.Label(top, text="● Flrig: offline", bg=BG, fg=WARN, font=SM)
        self._flrig_lbl.pack(side="right", padx=6)
        self._qrz_lbl = tk.Label(top, text="QRZ: —", bg=BG, fg=FG2, font=SM)
        self._qrz_lbl.pack(side="right", padx=6)
        self._mycall_lbl = tk.Label(top, text="", bg=BG, fg=ACC2, font=LBL)
        self._mycall_lbl.pack(side="right", padx=6)
        self._update_mycall_lbl()

        # VFO live bar
        vfo_bar = tk.Frame(self, bg=BG2)
        vfo_bar.pack(fill="x", padx=14, pady=(0,6))
        vi = tk.Frame(vfo_bar, bg=BG2)
        vi.pack(padx=12, pady=6, anchor="w")
        tk.Label(vi, text="RIG VFO", bg=BG2, fg=MUTED, font=SM).grid(row=0,column=0)
        self._vfo_freq = tk.Label(vi, text="—", bg=BG2, fg=ACCENT, font=DISP,
                                  width=14, anchor="w")
        self._vfo_freq.grid(row=0,column=1,padx=(8,20))
        tk.Label(vi, text="MODE", bg=BG2, fg=MUTED, font=SM).grid(row=0,column=2)
        self._vfo_mode = tk.Label(vi, text="—", bg=BG2, fg=ACC2,
                                  font=("Courier New",18,"bold"), width=8, anchor="w")
        self._vfo_mode.grid(row=0,column=3,padx=(6,20))
        tk.Label(vi, text="BAND", bg=BG2, fg=MUTED, font=SM).grid(row=0,column=4)
        self._vfo_band = tk.Label(vi, text="—", bg=BG2, fg=ACC3,
                                  font=("Courier New",14,"bold"), width=5, anchor="w")
        self._vfo_band.grid(row=0,column=5,padx=(6,20))
        tk.Label(vi, text="← captured automatically on LOG QSO",
                 bg=BG2, fg=MUTED, font=SM).grid(row=0,column=6,padx=4)

        # S-meter / power-meter bar
        meter_row = tk.Frame(vfo_bar, bg=BG2)
        meter_row.pack(fill="x", padx=12, pady=(0,6))
        self._meter_type_lbl = tk.Label(meter_row, text="S-METER", width=8,
                                        anchor="w", bg=BG2, fg=MUTED, font=SM)
        self._meter_type_lbl.pack(side="left")
        self._meter_canvas = tk.Canvas(meter_row, height=14, bg=BG3,
                                       bd=0, highlightthickness=0)
        self._meter_canvas.pack(side="left", fill="x", expand=True, padx=(4,4))
        self._meter_val_lbl = tk.Label(meter_row, text="  0%", width=5,
                                       anchor="e", bg=BG2, fg=MUTED, font=SM)
        self._meter_val_lbl.pack(side="left")
        self._meter_canvas.bind("<Configure>",
                                lambda e: self._draw_meter_bar(self._meter_value,
                                                               self._meter_is_tx))

        # Entry form
        form_frame = tk.LabelFrame(self, text=" NEW QSO ", bg=BG, fg=ACCENT,
                                   font=LBL, bd=1, relief="groove")
        form_frame.pack(fill="x", padx=14, pady=(0,6))
        self._build_entry_form(form_frame)

        # ── Notebook (3 tabs) ──────────────────────────────────────────────
        self._nb = ttk.Notebook(self)
        self._nb.pack(fill="both", expand=True, padx=14, pady=(0,4))

        tab1 = tk.Frame(self._nb, bg=BG)
        tab2 = tk.Frame(self._nb, bg=BG)
        tab3 = tk.Frame(self._nb, bg=MAP_BG)

        self._nb.add(tab3, text="  POTA Spots  ")
        self._nb.add(tab1, text="  QSO Log  ")
        self._nb.add(tab2, text="  Grid Map  ")

        self._build_tab_log(tab1)
        self._build_tab_map(tab2)
        self._build_tab_pota(tab3)

        self._nb.bind("<<NotebookTabChanged>>", self._on_tab_changed)

        # Bottom status
        bot = tk.Frame(self, bg=BG)
        bot.pack(fill="x", padx=14, pady=(0,4))
        self._status_var = tk.StringVar(value="Ready.")
        tk.Label(bot, textvariable=self._status_var, bg=BG, fg=FG2,
                 font=SM, anchor="w").pack(side="left")
        bc = dict(font=SM, relief="flat", cursor="hand2", pady=3, padx=10)
        tk.Button(bot, text="✎ Edit",   bg=BG3, fg=FG,
                  command=self._edit_qso,   **bc).pack(side="right")
        tk.Button(bot, text="✕ Delete", bg=WARN, fg=BG,
                  command=self._delete_qso, **bc).pack(side="right", padx=4)

    # ── Tab 1: QSO Log ────────────────────────────────────────────────────
    def _build_tab_log(self, parent):
        # Search bar
        srch = tk.Frame(parent, bg=BG)
        srch.pack(fill="x", padx=4, pady=(4,4))
        tk.Label(srch, text="SEARCH:", bg=BG, fg=FG2, font=LBL).pack(side="left")
        self._search_var = tk.StringVar()
        self._search_after_id = None
        def _debounced_filter(*_):
            if self._search_after_id:
                self.after_cancel(self._search_after_id)
            self._search_after_id = self.after(150, self._apply_filter)
        self._search_var.trace_add("write", _debounced_filter)
        tk.Entry(srch, textvariable=self._search_var, bg=BG3, fg=FG,
                 font=MONO, relief="flat", insertbackground=ACCENT, bd=4,
                 width=26).pack(side="left", padx=4)
        tk.Label(srch, text="BAND:", bg=BG, fg=FG2, font=LBL).pack(side="left", padx=(10,0))
        self._filter_band = ttk.Combobox(srch, values=["All"]+BANDS,
                                         width=7, font=SM, state="readonly")
        self._filter_band.set("All")
        self._filter_band.bind("<<ComboboxSelected>>", lambda _: self._apply_filter())
        self._filter_band.pack(side="left", padx=4)
        tk.Label(srch, text="MODE:", bg=BG, fg=FG2, font=LBL).pack(side="left", padx=(8,0))
        self._filter_mode = ttk.Combobox(srch, values=["All"]+MODES,
                                         width=8, font=SM, state="readonly")
        self._filter_mode.set("All")
        self._filter_mode.bind("<<ComboboxSelected>>", lambda _: self._apply_filter())
        self._filter_mode.pack(side="left", padx=4)
        tk.Button(srch, text="✕ Clear", bg=BG3, fg=FG2, font=SM,
                  relief="flat", cursor="hand2", padx=6,
                  command=self._clear_filter).pack(side="left", padx=6)
        self._qso_count_lbl = tk.Label(srch, text="0 QSOs", bg=BG, fg=FG2, font=SM)
        self._qso_count_lbl.pack(side="right")

        # Logbook table
        tbl = tk.Frame(parent, bg=BG)
        tbl.pack(fill="both", expand=True, padx=4, pady=(0,4))
        cols = ("id","Call","Date","Time UTC","Freq MHz","Band","Mode",
                "RST Snt","RST Rcv","Park #","Comments","Notes")
        self._tree = ttk.Treeview(tbl, columns=cols, show="headings",
                                  height=14, selectmode="browse")
        widths = [34,90,88,68,92,55,65,62,62,80,160,160]
        for col,w in zip(cols,widths):
            self._tree.heading(col, text=col,
                               command=lambda c=col: self._sort_by(c))
            self._tree.column(col, width=w, anchor="center", minwidth=28)
        vsb = tk.Scrollbar(tbl, orient="vertical",   command=self._tree.yview, bg=BG3)
        hsb = tk.Scrollbar(tbl, orient="horizontal", command=self._tree.xview, bg=BG3)
        self._tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        self._tree.grid(row=0,column=0,sticky="nsew")
        vsb.grid(row=0,column=1,sticky="ns")
        hsb.grid(row=1,column=0,sticky="ew")
        tbl.rowconfigure(0,weight=1); tbl.columnconfigure(0,weight=1)
        self._tree.tag_configure("odd",  background=BG2, foreground=FG)
        self._tree.tag_configure("even", background=BG3, foreground=FG)
        self._tree.bind("<<TreeviewSelect>>", self._on_qso_select)
        self._tree.bind("<Double-1>",         self._edit_qso)
        self._tree.bind("<Delete>",    lambda _: self._delete_qso())

    # ── Tab 2: Grid Map ───────────────────────────────────────────────────
    def _build_tab_map(self, parent):
        # Toolbar
        tb = tk.Frame(parent, bg=MAP_BG)
        tb.pack(fill="x", padx=6, pady=(4,2))
        tk.Label(tb, text="GRID MAP", bg=MAP_BG, fg=ACCENT, font=LBL).pack(side="left")
        self._map_count_lbl = tk.Label(tb, text="", bg=MAP_BG, fg=FG2, font=SM)
        self._map_count_lbl.pack(side="left", padx=12)
        ttk.Checkbutton(tb, text="Show Spots", variable=self._map_show_spots,
                        command=self._refresh_map).pack(side="left", padx=(4, 0))

        btn_kw = dict(bg=BG3, fg=FG, font=SM, relief="flat", cursor="hand2", padx=8)
        tk.Button(tb, text="⟳ Refresh", command=self._refresh_map,
                  **btn_kw).pack(side="right")
        tk.Button(tb, text="Open in Browser", command=self._open_leaflet_map,
                  **btn_kw).pack(side="right", padx=6)
        tk.Button(tb, text="⌂", command=self._map_zoom_reset,
                  **btn_kw).pack(side="right", padx=(0, 2))
        tk.Button(tb, text=" + ", command=self._map_zoom_in,
                  **btn_kw).pack(side="right", padx=(0, 2))
        tk.Button(tb, text=" − ", command=self._map_zoom_out,
                  **btn_kw).pack(side="right", padx=(0, 2))

        # Canvas
        self._map_canvas = tk.Canvas(parent, bg=MAP_BG,
                                     highlightthickness=0, bd=0)
        self._map_canvas.pack(fill="both", expand=True, padx=6, pady=(0,6))

        # Tooltip label (hidden until hover)
        self._map_tooltip = tk.Label(self._map_canvas, text="", bg=BG4, fg=FG,
                                     font=SM, relief="flat", bd=0,
                                     padx=6, pady=3)

        self._map_canvas.bind("<Configure>",      self._on_map_resize)
        self._map_canvas.bind("<Motion>",          self._on_map_motion)
        self._map_canvas.bind("<Leave>",           lambda _: self._map_tooltip.place_forget())
        self._map_canvas.bind("<MouseWheel>",      self._on_map_scroll)
        self._map_canvas.bind("<Button-4>",        self._on_map_scroll)
        self._map_canvas.bind("<Button-5>",        self._on_map_scroll)
        self._map_canvas.bind("<ButtonPress-1>",   self._on_map_drag_start)
        self._map_canvas.bind("<B1-Motion>",       self._on_map_drag)
        self._map_canvas.bind("<ButtonRelease-1>", self._on_map_drag_end)

    def _on_map_resize(self, _=None):
        if self._map_resize_id:
            self.after_cancel(self._map_resize_id)
        self._map_resize_id = self.after(120, self._full_map_redraw)

    def _full_map_redraw(self):
        canvas = self._map_canvas
        W = canvas.winfo_width()
        H = canvas.winfo_height()
        if W < 10 or H < 10:
            return
        canvas.delete("all")

        def px(lon, lat):
            x = (lon + 180) / 360 * W * self._map_zoom + self._map_pan_x
            y = (90  - lat) / 180 * H * self._map_zoom + self._map_pan_y
            return x, y

        # Faint Maidenhead grid lines
        for i in range(19):
            lon = -180 + i * 20
            x, _ = px(lon, 0)
            canvas.create_line(x, 0, x, H, fill=MAP_GRID, width=1, tags="static")
        for j in range(19):
            lat = 90 - j * 10
            _, y = px(0, lat)
            canvas.create_line(0, y, W, y, fill=MAP_GRID, width=1, tags="static")

        # Field column labels (A–R) along top
        for i in range(18):
            x, _ = px(-180 + i * 20 + 10, 0)
            canvas.create_text(x, 10, text=chr(ord('A') + i),
                               fill=MAP_GRID2, font=("Courier New", 7), tags="static")
        # Field row labels (R–A top to bottom) along left
        for j in range(18):
            _, y = px(0, 90 - j * 10 - 5)
            canvas.create_text(8, y, text=chr(ord('A') + 17 - j),
                               fill=MAP_GRID2, font=("Courier New", 7), tags="static")

        # Natural Earth 110m filled land polygons
        for ring in NE110_LAND:
            if len(ring) < 3:
                continue
            pts = []
            for lon, lat in ring:
                x, y = px(lon, lat)
                pts.extend((x, y))
            if len(pts) >= 6:
                canvas.create_polygon(pts, fill=MAP_LAND, outline=MAP_COAST,
                                      width=1, tags="static")

        self._map_drawn = True
        self._draw_map_markers(W, H)

    def _refresh_map(self):
        if not hasattr(self, '_map_canvas'):
            return
        canvas = self._map_canvas
        self.update_idletasks()
        W = canvas.winfo_width()
        H = canvas.winfo_height()
        if W < 10 or H < 10:
            return
        if not self._map_drawn:
            self._full_map_redraw()
            return
        self._draw_map_markers(W, H)

    def _read_adif_grids(self):
        """Read the active ADIF log file and return grouped grid square data."""
        if not self.adif_path or not os.path.exists(self.adif_path):
            return []
        try:
            with open(self.adif_path, "r", encoding="utf-8", errors="replace") as f:
                text = f.read()
        except OSError:
            return []
        records = parse_adif_records(text)
        groups = {}
        for rec in records:
            raw_gs = (rec.get("GRIDSQUARE", "") or rec.get("GRID", "")).strip().upper()[:6]
            if len(raw_gs) < 4:
                continue
            call = rec.get("CALL", "").strip()
            if raw_gs not in groups:
                groups[raw_gs] = {"calls": [], "cnt": 0}
            groups[raw_gs]["cnt"] += 1
            if call and call not in groups[raw_gs]["calls"]:
                groups[raw_gs]["calls"].append(call)
        return [{"gs": gs, "calls": ", ".join(v["calls"]), "cnt": v["cnt"]}
                for gs, v in groups.items()]

    def _draw_map_markers(self, W, H):
        canvas = self._map_canvas
        canvas.delete("marker")
        canvas.delete("mymarker")
        self._map_markers = {}
        self._map_marker_items = {}
        self._map_spot_data = {}

        def mpx(lon, lat):
            x = (lon + 180) / 360 * W * self._map_zoom + self._map_pan_x
            y = (90  - lat) / 180 * H * self._map_zoom + self._map_pan_y
            return x, y

        # ── Logged QSO grids ────────────────────────────────────────────
        qso_rows = self._read_adif_grids()
        logged_gs = {r["gs"] for r in qso_rows}
        qso_info  = {r["gs"]: r for r in qso_rows}

        # ── Active POTA spot grids ───────────────────────────────────────
        spot_gs_map  = {}   # gs → list of activator callsigns
        tuned_gs     = set()
        if self._map_show_spots.get() and self._pota_spots_filtered:
            refs = list({s.get("reference", s.get("parkReference", ""))
                         for s in self._pota_spots_filtered})
            if refs:
                placeholders = ",".join("?" * len(refs))
                try:
                    with sqlite3.connect(PARKS_DB) as _pk:
                        park_rows = _pk.execute(
                            f"SELECT reference, grid FROM parks "
                            f"WHERE reference IN ({placeholders})", refs).fetchall()
                    ref_to_grid = {r[0]: (r[1] or "")[:4] for r in park_rows if r[1]}
                except Exception:
                    ref_to_grid = {}

                for s in self._pota_spots_filtered:
                    ref = s.get("reference", s.get("parkReference", ""))
                    gs  = ref_to_grid.get(ref, "")
                    if len(gs) < 4:
                        continue
                    _slat, _slon = grid_to_latlon(gs)
                    if _slat is None:
                        continue
                    _sx, _sy = mpx(_slon, _slat)
                    self._map_spot_data.setdefault((round(_sx), round(_sy)), []).append(s)

                if time.monotonic() < self._tune_suppress_until and self._pota_clicked_hz:
                    vfo_hz = self._pota_clicked_hz
                else:
                    vfo_hz = (self._flrig_freq_hz if self._flrig_freq_hz is not None
                              else self._pota_clicked_hz)

                for s in self._pota_spots_filtered:
                    ref  = s.get("reference", s.get("parkReference", ""))
                    act  = s.get("activator",  s.get("activatorCallsign", ""))
                    gs   = ref_to_grid.get(ref, "")
                    if len(gs) < 4:
                        continue
                    spot_gs_map.setdefault(gs, [])
                    if act and act not in spot_gs_map[gs]:
                        spot_gs_map[gs].append(act)
                    if vfo_hz is not None:
                        try:
                            spot_hz = int(float(s.get("frequency", s.get("freq", 0))) * 1000)
                            if spot_hz == int(vfo_hz):
                                tuned_gs.add(gs)
                        except (ValueError, TypeError):
                            pass

        logged_gs_4 = {gs[:4] for gs in logged_gs}
        active_only_gs = {gs for gs in spot_gs_map if gs[:4] not in logged_gs_4 and gs not in tuned_gs}
        flash_gs = {gs for gs in logged_gs if gs[:4] in (set(spot_gs_map) - tuned_gs)}

        # ── Draw active-only (orange) ────────────────────────────────────
        for gs in active_only_gs:
            lat, lon = grid_to_latlon(gs)
            if lat is None:
                continue
            x, y = mpx(lon, lat)
            canvas.create_rectangle(x-7, y-5, x+7, y+5,
                                    fill=MAP_GLOW, outline="", tags="marker")
            inner = canvas.create_rectangle(x-5, y-3, x+5, y+3,
                                            fill=YELLOW, outline="", tags="marker")
            self._map_markers[(round(x), round(y))] = (
                f"{', '.join(spot_gs_map[gs])}  [{gs}]  (spot)")
            self._map_marker_items[gs] = [inner]

        # ── Draw logged QSOs (green) ─────────────────────────────────────
        total_qsos = 0
        for row in qso_rows:
            gs  = row["gs"]
            cnt = row["cnt"]
            total_qsos += cnt
            if gs in flash_gs or gs in tuned_gs:
                continue
            lat, lon = grid_to_latlon(gs)
            if lat is None:
                continue
            x, y = mpx(lon, lat)
            canvas.create_rectangle(x-7, y-5, x+7, y+5,
                                    fill=MAP_GLOW, outline="", tags="marker")
            inner = canvas.create_rectangle(x-5, y-3, x+5, y+3,
                                            fill=POTA_WORKED, outline="", tags="marker")
            if cnt > 1:
                canvas.create_text(x, y, text=str(cnt),
                                   fill=BG, font=("Courier New", 7, "bold"),
                                   tags="marker")
            self._map_markers[(round(x), round(y))] = (
                f"{row['calls']}  [{gs}]  ×{cnt}")
            self._map_marker_items[gs] = [inner]

        # ── Draw flash grids (green initially; tick will alternate) ─────
        for gs in flash_gs:
            row = qso_info.get(gs, {})
            cnt = row.get("cnt", 0)
            lat, lon = grid_to_latlon(gs)
            if lat is None:
                continue
            x, y = mpx(lon, lat)
            canvas.create_rectangle(x-7, y-5, x+7, y+5,
                                    fill=MAP_GLOW, outline="", tags="marker")
            inner = canvas.create_rectangle(x-5, y-3, x+5, y+3,
                                            fill=POTA_WORKED, outline="", tags="marker")
            if cnt > 1:
                canvas.create_text(x, y, text=str(cnt),
                                   fill=BG, font=("Courier New", 7, "bold"),
                                   tags="marker")
            self._map_markers[(round(x), round(y))] = (
                f"{row.get('calls', '')}  [{gs}]  ×{cnt}  +spot")
            self._map_marker_items[gs] = [inner]

        # ── Draw tuned (blue) ────────────────────────────────────────────
        self._map_tuned_pxs = []
        for gs in tuned_gs:
            lat, lon = grid_to_latlon(gs)
            if lat is None:
                continue
            x, y = mpx(lon, lat)
            canvas.create_rectangle(x-7, y-5, x+7, y+5,
                                    fill=MAP_GLOW, outline="", tags="marker")
            canvas.create_rectangle(x-5, y-3, x+5, y+3,
                                    fill=POTA_TUNED, outline="", tags="marker")
            label_parts = []
            if gs in spot_gs_map:
                label_parts.append(", ".join(spot_gs_map[gs]))
            if gs in qso_info:
                label_parts.append(f"×{qso_info[gs]['cnt']} QSO")
            self._map_markers[(round(x), round(y))] = (
                f"{' | '.join(label_parts)}  [{gs}]  tuned")
            self._map_tuned_pxs.append((x, y))

        # ── User's own grid (red diamond) ───────────────────────────────
        self._map_my_px = None
        my_gs = (self.cfg.get("gridsquare") or "")[:6].strip().upper()
        if len(my_gs) >= 4:
            lat, lon = grid_to_latlon(my_gs)
            if lat is not None:
                x, y = mpx(lon, lat)
                s = 10
                canvas.create_polygon(x, y-s, x+s, y, x, y+s, x-s, y,
                                      fill="#ff3333", outline="#ff8888",
                                      width=1, tags="mymarker")
                self._map_my_px = (x, y)
                self._map_markers[(round(x), round(y))] = (
                    f"My grid: {my_gs}")

        # ── Manage flash and beam loops ──────────────────────────────────
        self._map_flash_grids = flash_gs
        if flash_gs and not self._map_flash_id:
            self._map_flash_tick()
        self._map_spot_flash_grids = active_only_gs
        if active_only_gs and not self._map_spot_flash_id:
            self._map_spot_flash_tick()
        if self._map_my_px and self._map_tuned_pxs:
            self._start_map_beam()
        else:
            self._stop_map_beam()

        n = len(logged_gs)
        self._map_count_lbl.config(
            text=f"{n} grid square{'s' if n != 1 else ''} ({total_qsos} QSO{'s' if total_qsos != 1 else ''})")

    def _on_map_motion(self, event):
        canvas = self._map_canvas
        closest_label = None
        min_dist = 18
        for (cx, cy), label in self._map_markers.items():
            d = ((event.x - cx) ** 2 + (event.y - cy) ** 2) ** 0.5
            if d < min_dist:
                min_dist = d
                closest_label = label
        if closest_label:
            self._map_tooltip.config(text=closest_label)
            tx = min(event.x + 12, canvas.winfo_width() - 220)
            ty = max(event.y - 22, 4)
            self._map_tooltip.place(x=tx, y=ty)
        else:
            self._map_tooltip.place_forget()

    # ── Map zoom / pan ────────────────────────────────────────────────────

    def _map_zoom_in(self):
        self._map_zoom = min(self._map_zoom * 1.5, 20.0)
        self._full_map_redraw()

    def _map_zoom_out(self):
        self._map_zoom = max(self._map_zoom / 1.5, 1.0)
        if self._map_zoom <= 1.0:
            self._map_zoom = 1.0
            self._map_pan_x = self._map_pan_y = 0.0
        self._full_map_redraw()

    def _map_zoom_reset(self):
        self._map_zoom = 1.0
        self._map_pan_x = self._map_pan_y = 0.0
        self._full_map_redraw()

    def _on_map_scroll(self, event):
        factor = 1.25 if (getattr(event, "delta", 0) > 0 or getattr(event, "num", 0) == 4) else 0.8
        new_zoom = max(1.0, min(20.0, self._map_zoom * factor))
        if new_zoom == self._map_zoom:
            return
        mx, my = event.x, event.y
        self._map_pan_x = mx - (mx - self._map_pan_x) * new_zoom / self._map_zoom
        self._map_pan_y = my - (my - self._map_pan_y) * new_zoom / self._map_zoom
        self._map_zoom = new_zoom
        self._full_map_redraw()

    def _on_map_drag_start(self, event):
        self._map_drag_start   = (event.x, event.y)
        self._map_click_origin = (event.x, event.y)

    def _on_map_drag(self, event):
        if self._map_drag_start is None:
            return
        dx = event.x - self._map_drag_start[0]
        dy = event.y - self._map_drag_start[1]
        self._map_pan_x += dx
        self._map_pan_y += dy
        self._map_drag_start = (event.x, event.y)
        self._full_map_redraw()

    def _on_map_drag_end(self, event):
        origin = self._map_click_origin
        self._map_drag_start   = None
        self._map_click_origin = None
        if origin and abs(event.x - origin[0]) < 6 and abs(event.y - origin[1]) < 6:
            self._on_map_canvas_click(event)

    def _on_map_canvas_click(self, event):
        cx, cy = event.x, event.y
        best_key, best_dist = None, 18
        for (mx, my) in self._map_spot_data:
            d = ((cx - mx) ** 2 + (cy - my) ** 2) ** 0.5
            if d < best_dist:
                best_dist = d
                best_key = (mx, my)
        if best_key is None:
            self._toggle_pota_scan()
            return
        spots = self._map_spot_data[best_key]
        if not spots:
            return
        if time.monotonic() < self._tune_suppress_until and self._pota_clicked_hz:
            vfo_hz = self._pota_clicked_hz
        else:
            vfo_hz = self._flrig_freq_hz
        try:
            worked = {r[0] for r in self.conn.execute(
                "SELECT DISTINCT UPPER(TRIM(call)) FROM qso").fetchall() if r[0]}
        except Exception:
            worked = set()
        chosen = next(
            (s for s in spots
             if str(s.get("activator", s.get("activatorCallsign", ""))).upper() not in worked),
            spots[0]
        )
        try:
            freq_khz = float(chosen.get("frequency", chosen.get("freq", 0)))
            freq_hz  = int(freq_khz * 1_000)
        except (ValueError, TypeError):
            return
        already_tuned = vfo_hz is not None and freq_hz == int(vfo_hz)
        self._on_map_station_click({
            "activator": chosen.get("activator", chosen.get("activatorCallsign", "")),
            "park":      chosen.get("reference", chosen.get("parkReference", "")),
            "freq_khz":  freq_khz,
            "mode":      chosen.get("mode", ""),
            "tuned":     already_tuned,
        })

    # ── Map animations ────────────────────────────────────────────────────

    def _map_flash_tick(self):
        if not self._map_flash_grids:
            self._map_flash_id = None
            return
        self._map_flash_state = not self._map_flash_state
        color = POTA_WORKED if self._map_flash_state else ACCENT
        for gs in self._map_flash_grids:
            for item_id in self._map_marker_items.get(gs, []):
                try:
                    self._map_canvas.itemconfig(item_id, fill=color)
                except Exception:
                    pass
        self._map_flash_id = self.after(750, self._map_flash_tick)

    def _map_spot_flash_tick(self):
        if not self._map_spot_flash_grids:
            self._map_spot_flash_id = None
            return
        self._map_spot_flash_state = not self._map_spot_flash_state
        color = YELLOW if self._map_spot_flash_state else MAP_GLOW
        for gs in self._map_spot_flash_grids:
            for item_id in self._map_marker_items.get(gs, []):
                try:
                    self._map_canvas.itemconfig(item_id, fill=color)
                except Exception:
                    pass
        self._map_spot_flash_id = self.after(750, self._map_spot_flash_tick)

    def _map_scan_blink_tick(self):
        if not self._pota_scan_active:
            if hasattr(self, '_map_canvas'):
                self._map_canvas.delete("scan_label")
            self._map_scan_blink_id = None
            return
        self._map_scan_blink_st = not self._map_scan_blink_st
        self._map_canvas.delete("scan_label")
        if self._map_scan_blink_st:
            W = self._map_canvas.winfo_width()
            self._map_canvas.create_text(
                W // 2, 18, text="◈ SCANNING ◈",
                fill=POTA_TUNED, font=("Courier New", 11, "bold"),
                tags="scan_label")
        self._map_scan_blink_id = self.after(600, self._map_scan_blink_tick)

    def _start_map_scan_blink(self):
        if self._map_scan_blink_id:
            return
        self._map_scan_blink_tick()

    def _map_beam_tick(self):
        import math
        canvas = self._map_canvas
        canvas.delete("beam_line")
        if not self._map_my_px or not self._map_tuned_pxs:
            self._map_beam_id = None
            return
        x1, y1 = self._map_my_px
        step = 12
        dash_len = 6
        phase_offset = (self._map_beam_phase * 2) % step
        for (x2, y2) in self._map_tuned_pxs:
            dx = x2 - x1
            dy = y2 - y1
            length = math.hypot(dx, dy)
            if length < 1:
                continue
            d = phase_offset
            while d < length:
                d2 = min(d + dash_len, length)
                lx1 = x1 + dx / length * d
                ly1 = y1 + dy / length * d
                lx2 = x1 + dx / length * d2
                ly2 = y1 + dy / length * d2
                canvas.create_line(lx1, ly1, lx2, ly2,
                                   fill=POTA_TUNED, width=2, tags="beam_line")
                d += step
        self._map_beam_phase = (self._map_beam_phase + 1) % 6
        self._map_beam_id = self.after(80, self._map_beam_tick)

    def _start_map_beam(self):
        if self._map_beam_id:
            return
        self._map_beam_tick()

    def _stop_map_beam(self):
        if self._map_beam_id:
            self.after_cancel(self._map_beam_id)
            self._map_beam_id = None
        if hasattr(self, '_map_canvas'):
            self._map_canvas.delete("beam_line")

    def _open_leaflet_map(self):
        if not self._pota_loaded:
            self._pota_loaded = True
            threading.Thread(target=self._fetch_pota_spots, daemon=True).start()
        if self._map_server is None:
            self._start_map_server()
        if self._map_server_port:
            webbrowser.open(f"http://localhost:{self._map_server_port}")

    def _start_map_server(self):
        app = self
        MAP_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta http-equiv="Cache-Control" content="no-store, no-cache, must-revalidate">
<title>POTA Hunter — Live Map</title>
<link href="https://fonts.googleapis.com/css2?family=Roboto+Mono:wght@400;700&family=Orbitron:wght@400;700;900&display=swap" rel="stylesheet">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{--red:#ff2020;--red-dim:#8b0000;--amber:#ff9900;--green:#00ff88;--cyan:#00e5ff;--bg:#030609;--panel:#070d12;--border:#1a3040;--text:#c8dde8;--dim:#3a5060;}
:root.day-mode{--red:#990000;--red-dim:#6a1010;--amber:#7a3d00;--green:#004d22;--cyan:#003d77;--bg:#dfe6ed;--panel:#b8c8d8;--border:#4a7090;--text:#080e14;--dim:#2a4860;}
*{margin:0;padding:0;box-sizing:border-box;}
html,body{height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:'Roboto Mono',Consolas,monospace;}
body::before{content:'';position:fixed;inset:0;pointer-events:none;z-index:9000;background:repeating-linear-gradient(0deg,transparent,transparent 2px,rgba(0,0,0,.07) 2px,rgba(0,0,0,.07) 4px);}
.day-mode body::before{display:none;}
header{height:62px;display:flex;align-items:center;justify-content:space-between;padding:0 24px;border-bottom:1px solid var(--red);background:linear-gradient(90deg,#0a0002,#0d0008,#0a0002);position:relative;z-index:1000;flex-shrink:0;}
.day-mode header{background:linear-gradient(90deg,#f5e8e8,#f8eeee,#f5e8e8);}
header::after{content:'';position:absolute;inset:0;pointer-events:none;background:repeating-linear-gradient(90deg,transparent,transparent 60px,rgba(255,20,20,.025) 60px,rgba(255,20,20,.025) 61px);}
#theme-btn{cursor:pointer;font-family:'Orbitron',sans-serif;font-size:.6rem;letter-spacing:2px;padding:4px 12px;border:1px solid var(--dim);color:var(--dim);background:transparent;transition:all .2s;user-select:none;margin-left:8px;}
#theme-btn:hover{border-color:var(--amber);color:var(--amber);}
.logo{font-family:'Orbitron',sans-serif;font-weight:900;font-size:1.2rem;color:var(--red);letter-spacing:4px;text-shadow:0 0 20px rgba(255,32,32,.8),0 0 40px rgba(255,32,32,.3);}
.logo span{color:#fff;}
.hdr-mid{display:flex;gap:36px;align-items:center;font-size:.85rem;letter-spacing:2px;color:var(--dim);}
.status-dot{width:11px;height:11px;border-radius:50%;display:inline-block;margin-right:5px;}
.status-dot.connected{background:var(--green);box-shadow:0 0 8px var(--green);animation:sdpulse-green 1.5s ease-in-out infinite;}
.status-dot.offline{background:var(--red);box-shadow:0 0 6px var(--red);animation:sdpulse-red 1.5s ease-in-out infinite;}
@keyframes sdpulse-green{0%,100%{opacity:1;box-shadow:0 0 8px var(--green)}50%{opacity:.4;box-shadow:0 0 2px var(--green)}}
@keyframes sdpulse-red{0%,100%{opacity:1;box-shadow:0 0 8px var(--red)}50%{opacity:.4;box-shadow:0 0 2px var(--red)}}
#clock{font-family:'Orbitron',sans-serif;font-size:.95rem;color:var(--amber);letter-spacing:2px;}
.app-body{display:flex;height:calc(100vh - 62px);}
.panel{width:240px;flex-shrink:0;background:var(--panel);border-right:1px solid var(--border);display:flex;flex-direction:column;overflow:hidden;}
.panel.right{width:260px;border-right:none;border-left:1px solid var(--border);}
.panel-inner{flex:1;overflow-y:auto;padding:13px;display:flex;flex-direction:column;gap:9px;}
.panel-title{font-family:'Orbitron',sans-serif;font-size:.57rem;letter-spacing:3px;color:var(--red);text-transform:uppercase;padding-bottom:6px;border-bottom:1px solid var(--red-dim);flex-shrink:0;}
.card{background:rgba(255,255,255,.02);border:1px solid var(--border);padding:8px 11px;position:relative;flex-shrink:0;}
.card::before{content:'';position:absolute;top:0;left:0;width:3px;height:100%;background:var(--red);}
.card-label{font-size:.57rem;letter-spacing:2px;color:var(--dim);text-transform:uppercase;margin-bottom:3px;}
.card-value{font-family:'Orbitron',sans-serif;font-size:1rem;color:var(--amber);text-shadow:0 0 10px rgba(255,153,0,.5);}
.card-sub{font-size:.6rem;color:var(--dim);margin-top:2px;}
.chips{display:flex;flex-wrap:wrap;gap:3px;}
.chip{border:1px solid;padding:2px 7px;font-size:.56rem;letter-spacing:1px;}
.chip strong{color:var(--amber);}
.map-area{flex:1;position:relative;overflow:hidden;}
#map{width:100%;height:100%;background:#020810;}
.spot-item{background:rgba(255,255,255,.015);border:1px solid var(--border);padding:7px 10px;cursor:pointer;transition:all .15s;flex-shrink:0;border-left:3px solid var(--dim);}
.spot-item:hover{background:rgba(0,229,255,.07);box-shadow:0 0 10px rgba(0,229,255,.1);}
.spot-item.tuned{border-left-color:var(--cyan);background:rgba(0,229,255,.05);}
.spot-item.tuned:hover{background:rgba(0,229,255,.09);}
.spot-item.worked{border-left-color:#00bb44;}
.spot-item.worked:hover{background:rgba(0,187,68,.07);}
.spot-call{font-family:'Roboto Mono',Consolas,monospace;font-size:.72rem;color:var(--cyan);text-shadow:0 0 6px rgba(0,229,255,.4);display:flex;align-items:center;justify-content:space-between;}
.spot-badge{font-size:.52rem;letter-spacing:1px;padding:1px 5px;border:1px solid currentColor;}
.spot-badge.tuned{color:var(--cyan);}
.spot-badge.worked{color:#00bb44;}
.spot-meta{font-size:.58rem;color:var(--dim);margin-top:3px;display:flex;gap:7px;flex-wrap:wrap;}
.spot-park{color:var(--amber);}
.no-spots{text-align:center;padding:28px 10px;color:var(--dim);font-size:.6rem;letter-spacing:2px;line-height:2;}
.beam-anim{animation:beam-flow 0.9s linear infinite;}
@keyframes beam-flow{to{stroke-dashoffset:-20;}}
@keyframes spot-flash{0%,100%{opacity:1}50%{opacity:0.1}}
.spot-flash{animation:spot-flash 2.5s ease-in-out infinite;}
::-webkit-scrollbar{width:3px;}
::-webkit-scrollbar-thumb{background:var(--red-dim);}
#scan-btn{cursor:pointer;font-family:'Orbitron',sans-serif;font-size:.6rem;letter-spacing:2px;padding:4px 12px;border:1px solid currentColor;transition:all .2s;user-select:none;}
#scan-btn.active{color:var(--green);border-color:var(--green);text-shadow:0 0 8px var(--green);}
#scan-btn.paused{color:var(--red);border-color:var(--red-dim);}
#scan-overlay{position:absolute;top:8px;left:50%;transform:translateX(-50%);z-index:1000;font-family:'Orbitron',sans-serif;font-size:.85rem;letter-spacing:4px;color:#00e5ff;text-shadow:0 0 12px #00e5ff;background:rgba(3,6,9,.8);padding:4px 18px;border:1px solid #00e5ff;pointer-events:none;display:none;white-space:nowrap;}
#scan-dots{display:inline-block;width:2ch;text-align:left;}
.stn-box{border:1px solid;padding:7px 10px;margin-top:3px;flex-shrink:0;}
.stn-box.logged{border-color:var(--green);background:rgba(0,255,136,.04);}
.stn-box.tuned-s{border-color:var(--cyan);background:rgba(0,229,255,.04);}
.stn-call{font-family:'Roboto Mono',Consolas,monospace;font-size:.72rem;letter-spacing:1px;}
.stn-call.logged{color:var(--green);text-shadow:0 0 6px rgba(0,255,136,.4);}
.stn-call.tuned-s{color:var(--cyan);text-shadow:0 0 6px rgba(0,229,255,.4);}
.day-mode .stn-box.logged{background:rgba(0,77,34,.08);}
.day-mode .stn-call.logged{text-shadow:none;}
.day-mode .stn-box.tuned-s{background:rgba(0,61,119,.08);}
.day-mode .stn-call.tuned-s{text-shadow:none;}
.stn-detail{font-size:.56rem;color:var(--dim);margin-top:3px;line-height:1.7;}
.stn-detail span{display:block;}
#radar-btn{cursor:pointer;font-family:'Orbitron',sans-serif;font-size:.6rem;letter-spacing:2px;padding:4px 12px;border:1px solid currentColor;transition:all .2s;user-select:none;margin-left:8px;}
#radar-btn.off{color:var(--dim);border-color:var(--dim);}
#radar-btn.on{color:var(--cyan);border-color:var(--cyan);text-shadow:0 0 8px rgba(0,229,255,.7);}
#log-modal-overlay{position:fixed;inset:0;z-index:9500;background:rgba(0,0,0,.82);
  display:none;align-items:center;justify-content:center;}
#log-modal{background:var(--panel);border:1px solid var(--cyan);padding:22px 26px;width:340px;
  font-family:'Roboto Mono',Consolas,monospace;position:relative;
  box-shadow:0 0 40px rgba(0,229,255,.18);}
#log-modal.activator-mode{width:460px;padding:26px 32px;}
#log-modal::before{content:'';position:absolute;top:0;left:0;width:3px;height:100%;background:var(--cyan);}
.lm-title{font-family:'Orbitron',sans-serif;font-size:.9rem;letter-spacing:3px;color:var(--cyan);
  margin-bottom:16px;text-transform:uppercase;text-shadow:0 0 10px rgba(0,229,255,.5);}
.activator-mode .lm-title{font-size:1.05rem;}
.lm-row{display:flex;flex-direction:column;gap:3px;margin-bottom:11px;}
.lm-label{font-size:.7rem;letter-spacing:2px;color:var(--dim);text-transform:uppercase;}
.activator-mode .lm-label{font-size:.82rem;}
.lm-input{background:rgba(0,229,255,.04);border:1px solid var(--border);color:var(--text);
  font-family:'Roboto Mono',Consolas,monospace;font-size:.9rem;padding:5px 8px;outline:none;width:100%;
  box-sizing:border-box;transition:border-color .15s;}
.activator-mode .lm-input{font-size:1.05rem;padding:7px 10px;}
.lm-input:focus{border-color:var(--cyan);box-shadow:0 0 6px rgba(0,229,255,.2);}
.lm-input[readonly]{color:var(--dim);cursor:default;}
.lm-row-2{display:grid;grid-template-columns:1fr 1fr;gap:10px;}
.lm-btns{display:flex;gap:10px;margin-top:16px;}
.lm-btn{flex:1;font-family:'Orbitron',sans-serif;font-size:.6rem;letter-spacing:2px;padding:7px 0;
  border:1px solid;cursor:pointer;transition:all .15s;text-transform:uppercase;background:transparent;}
.lm-btn.confirm{color:var(--green);border-color:var(--green);}
.lm-btn.confirm:hover{background:rgba(0,255,136,.1);box-shadow:0 0 10px rgba(0,255,136,.2);}
.lm-btn.confirm:disabled{opacity:.35;cursor:not-allowed;}
.lm-btn.cancel{color:#ff4040;border-color:#6a2020;}
.lm-btn.cancel:hover{background:rgba(255,32,32,.08);}
.lm-btn.selfspot{color:var(--amber);border-color:#7a5000;}
.lm-btn.selfspot:hover{background:rgba(255,153,0,.1);box-shadow:0 0 8px rgba(255,153,0,.15);}
.lm-btn.selfspot:disabled{opacity:.35;cursor:not-allowed;}
.lm-btn.clear{color:var(--cyan);border-color:#005a6e;}
.lm-btn.clear:hover{background:rgba(0,229,255,.08);box-shadow:0 0 8px rgba(0,229,255,.1);}
.lm-btn.multiop{color:#cc88ff;border-color:#5a2080;}
.lm-btn.multiop:hover{background:rgba(200,100,255,.08);box-shadow:0 0 8px rgba(200,100,255,.1);}
.lm-btn.multiop:disabled{opacity:.35;cursor:not-allowed;}
#lm-status{font-size:.6rem;letter-spacing:1px;min-height:1.4em;text-align:center;margin-top:8px;}
#lm-status.ok{color:var(--green);}
#lm-status.err{color:#ff4040;}
#ts-log-btn{display:block;margin-top:8px;width:100%;font-family:'Orbitron',sans-serif;
  font-size:.55rem;letter-spacing:2px;padding:5px 0;border:1px solid var(--cyan);
  color:var(--cyan);background:transparent;cursor:pointer;text-transform:uppercase;transition:all .15s;}
#ts-log-btn:hover{background:rgba(0,229,255,.1);box-shadow:0 0 8px rgba(0,229,255,.15);}
.stn-box.tuned-s{cursor:pointer;}
.stn-box.tuned-s:hover{background:rgba(0,229,255,.08);box-shadow:0 0 8px rgba(0,229,255,.1);}
.day-mode .stn-box.tuned-s:hover{background:rgba(0,61,119,.12);box-shadow:0 0 8px rgba(0,61,119,.2);}
.spot-item .spot-dbl-hint{font-size:.48rem;color:var(--dim);letter-spacing:1px;
  margin-top:2px;opacity:0;transition:opacity .2s;}
.spot-item:hover .spot-dbl-hint{opacity:1;}
.mode-btn{display:block;width:100%;margin-top:10px;font-family:'Orbitron',sans-serif;
  font-size:.55rem;letter-spacing:2px;padding:6px 0;
  border:1px solid #ff2020;color:#ff2020;
  background:transparent;cursor:pointer;text-transform:uppercase;transition:all .2s;
  box-shadow:0 0 8px rgba(255,32,32,.15);}
.mode-btn.activator{border-color:var(--green);color:var(--green);
  box-shadow:0 0 8px rgba(0,255,136,.2);}
.mode-btn:hover{opacity:.85;}
#respot-btn{display:block;width:100%;margin-top:6px;font-family:'Orbitron',sans-serif;
  font-size:.55rem;letter-spacing:2px;padding:6px 0;border:1px solid var(--dim);
  color:var(--dim);background:transparent;cursor:pointer;text-transform:uppercase;transition:all .2s;}
#respot-btn:hover{border-color:#00e5ff;color:#00e5ff;}
#respot-panel{margin-top:6px;padding:8px 10px;border:1px solid var(--border);
  background:rgba(0,229,255,.03);display:none;}
.respot-row{display:flex;gap:6px;align-items:center;margin-bottom:6px;}
.respot-input{flex:1;background:rgba(0,229,255,.04);border:1px solid var(--border);
  color:var(--text);font-family:'Roboto Mono',Consolas,monospace;font-size:.75rem;
  padding:4px 7px;outline:none;transition:border-color .15s;}
.respot-input:focus{border-color:var(--cyan);}
.respot-go{font-family:'Orbitron',sans-serif;font-size:.5rem;letter-spacing:1px;
  padding:4px 8px;border:1px solid var(--cyan);color:var(--cyan);background:transparent;
  cursor:pointer;text-transform:uppercase;white-space:nowrap;transition:all .15s;}
.respot-go:hover{background:rgba(0,229,255,.1);}
.respot-go:disabled{opacity:.35;cursor:not-allowed;}
#respot-status{font-size:.55rem;letter-spacing:1px;min-height:1.2em;}
#respot-status.ok{color:var(--green);}
#respot-status.err{color:#ff4040;}
/* ── Day-mode readability overrides ─────────────────────────── */
.day-mode .card{background:rgba(255,255,255,.5);}
.day-mode .card-value{text-shadow:none;}
.day-mode .spot-item{background:rgba(255,255,255,.4);}
.day-mode .spot-item:hover{background:rgba(0,61,119,.1);box-shadow:none;}
.day-mode .spot-item.tuned{background:rgba(0,61,119,.1);}
.day-mode .spot-item.tuned:hover{background:rgba(0,61,119,.15);}
.day-mode .spot-call{text-shadow:none;}
.day-mode .lm-input{background:#fff;}
.day-mode .lm-input:focus{box-shadow:0 0 4px rgba(0,61,119,.35);}
.day-mode .respot-input{background:#fff;}
.day-mode #respot-panel{background:rgba(255,255,255,.35);}
.day-mode #log-modal{box-shadow:0 4px 24px rgba(0,0,0,.28);}
.day-mode .lm-title{text-shadow:none;}
.day-mode .logo{text-shadow:none;}
.day-mode #scan-btn.active{text-shadow:none;}
.day-mode #radar-btn.on{text-shadow:none;}
.day-mode .mode-btn{box-shadow:none;}
.day-mode #respot-btn:hover{border-color:var(--cyan);color:var(--cyan);}
.day-mode #scan-overlay{background:rgba(220,230,240,.92);color:var(--cyan);border-color:var(--cyan);text-shadow:none;}
.day-mode .spot-item.worked{border-left-color:#1a6600;}
.day-mode .spot-item.worked:hover{background:rgba(26,102,0,.12);}
.day-mode .spot-badge.worked{color:#1a6600;font-weight:bold;}
/* ── VFO display ─────────────────────────────────────────────── */
#vfo-display{transition:color .4s;font-size:.85rem;color:var(--dim);}
#vfo-display.live{color:var(--green);font-size:1.1rem;text-shadow:0 0 6px rgba(0,255,136,.4);}
.day-mode #vfo-display.live{font-size:.85rem;text-shadow:none;}
/* ── Activator banner ────────────────────────────────────────── */
#activator-banner{display:none;position:absolute;top:28px;left:0;right:0;
  text-align:center;font-family:'Orbitron',sans-serif;font-size:2.2rem;font-weight:900;
  letter-spacing:8px;color:#ff2020;
  text-shadow:0 0 30px rgba(255,32,32,.9),0 0 60px rgba(255,32,32,.5);
  user-select:none;pointer-events:none;}
.day-mode #activator-banner{text-shadow:none;color:#990000;}
@keyframes banner-flash{0%{opacity:1;}49%{opacity:1;}50%{opacity:0;}99%{opacity:0;}100%{opacity:1;}}
.banner-flashing{animation:banner-flash 1s linear 3 forwards;}
</style>
</head>
<body>
<header>
  <div class="logo">// <span>POTA Hunter</span></div>
  <div class="hdr-mid">
    <span><span class="status-dot offline" id="status-dot"></span><span id="status-text">OFFLINE</span></span>
    <span id="clock">--:--:-- ZULU</span>
    <span id="mycall" style="color:var(--cyan);font-family:'Orbitron',sans-serif;font-size:.95rem;letter-spacing:3px;"></span>
    <span id="vfo-display" style="font-family:'Orbitron',sans-serif;letter-spacing:2px;">— · —</span>
  </div>
  <div style="display:flex;align-items:center;gap:0;">
    <div id="scan-btn" class="paused">⏸ SCAN PAUSED</div>
    <div id="radar-btn" class="off">◎ RADAR OFF</div>
    <button id="theme-btn" onclick="toggleMapTheme()">&#9788; DAY</button>
  </div>
</header>
<div class="app-body">
  <div class="panel">
    <div class="panel-inner">
      <div class="panel-title">◈ POTA STATUS</div>
      <div class="card">
        <div class="card-label">Active Spots</div>
        <div class="card-value" id="stat-spots">—</div>
        <div class="card-sub">Live POTA activations</div>
      </div>
      <div class="card">
        <div class="card-label">QSOs Logged</div>
        <div class="card-value" id="stat-qsos">—</div>
        <div class="card-sub">This session</div>
      </div>
      <div class="panel-title" style="margin-top:4px">◈ BANDS</div>
      <div class="chips" id="stat-bands">
        <div style="color:var(--dim);font-size:.6rem;letter-spacing:2px">NO SPOTS</div>
      </div>
      <div class="panel-title" style="margin-top:6px">◈ TUNED STATION</div>
      <div id="tuned-station-box" class="stn-box tuned-s" style="display:none" title="Double-click to log QSO" ondblclick="openLogModal(_tunedCardSpot)">
        <div class="stn-call tuned-s" id="ts-call"></div>
        <div class="stn-detail" id="ts-detail"></div>
        <button id="ts-log-btn" onclick="event.stopPropagation();openLogModal(_tunedCardSpot)">&#9998; LOG QSO</button>
      </div>
      <div style="color:var(--dim);font-size:.56rem;letter-spacing:1px" id="ts-empty">NO TUNED STATION</div>
      <div class="panel-title" style="margin-top:6px">◈ LAST LOGGED</div>
      <div id="last-logged-box" class="stn-box logged" style="display:none">
        <div class="stn-call logged" id="ll-call"></div>
        <div class="stn-detail" id="ll-detail"></div>
      </div>
      <div style="color:var(--dim);font-size:.56rem;letter-spacing:1px" id="ll-empty">NO LOGGED QSOs</div>
      <button id="mode-toggle-btn" class="mode-btn" onclick="toggleMode()">ACTIVATOR MODE</button>
      <button id="respot-btn" onclick="toggleRespoPanel()">&#8635; RE-SPOT MY STATION</button>
      <div id="respot-panel">
        <div class="respot-row">
          <input class="respot-input" id="respot-park" type="text" placeholder="PARK REF" autocomplete="off" spellcheck="false">
        </div>
        <div class="respot-row" style="margin-top:5px">
          <input class="respot-input" id="respot-freq" type="text" placeholder="FREQ (kHz)" autocomplete="off" spellcheck="false">
          <input class="respot-input" id="respot-mode" type="text" placeholder="MODE" autocomplete="off" spellcheck="false" style="max-width:80px">
        </div>
        <div class="respot-row" style="margin-top:5px">
          <input class="respot-input" id="respot-comment" type="text" placeholder="COMMENT (optional)" autocomplete="off" spellcheck="false">
          <button class="respot-go" id="respot-go-btn" onclick="doRespot()">&#9654; SPOT</button>
        </div>
        <div id="respot-status"></div>
      </div>
    </div>
  </div>
  <div class="map-area">
    <div id="scan-overlay">SCANNING<span id="scan-dots">.</span></div>
    <div id="oob-overlay" style="display:none;position:fixed;inset:0;z-index:9999;background:rgba(0,0,0,.6);align-items:center;justify-content:center;">
      <div style="font-family:'Orbitron',sans-serif;font-size:2rem;letter-spacing:6px;color:#ff2020;text-shadow:0 0 30px #ff2020;border:2px solid #ff2020;padding:24px 48px;background:rgba(3,6,9,.95);">&#9888; OUT OF BAND</div>
    </div>
    <div id="map"></div>
  </div>
  <div class="panel right">
    <div class="panel-inner">
      <div class="panel-title">◈ ACTIVE SPOTS</div>
      <div id="spots-list">
        <div class="no-spots">AWAITING SPOTS...<br><br>Enable POTA scan to<br>populate this panel</div>
      </div>
    </div>
  </div>
</div>
<script>
var map=L.map('map',{center:[20,0],zoom:2});
var _tileLayer=L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',{
  attribution:'&copy; OpenStreetMap contributors &copy; CARTO',
  subdomains:'abcd',maxZoom:19});
_tileLayer.addTo(map);
var _dayMode=(localStorage.getItem('mapTheme')==='day');
function toggleMapTheme(){
  _dayMode=!_dayMode;
  localStorage.setItem('mapTheme',_dayMode?'day':'night');
  _applyMapTheme();}
function _applyMapTheme(){
  var root=document.documentElement;
  var btn=document.getElementById('theme-btn');
  if(_dayMode){
    root.classList.add('day-mode');
    btn.textContent='☽ NIGHT';
    _tileLayer.setUrl('https://{s}.basemaps.cartocdn.com/light_all/{z}/{x}/{y}{r}.png');
  }else{
    root.classList.remove('day-mode');
    btn.textContent='☀ DAY';
    _tileLayer.setUrl('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png');}}
_applyMapTheme();
var radarLayer=null,radarEnabled=false,radarRefreshTimer=null;
var RADAR_OPACITY=0.5;
function buildRadarUrl(host,path){return host+path+'/256/{z}/{x}/{y}/6/1_1.png';}
function applyRadarLayer(host,path){
  var url=buildRadarUrl(host,path);
  if(radarLayer){radarLayer.setUrl(url);}
  else{radarLayer=L.tileLayer(url,{opacity:RADAR_OPACITY,attribution:'Weather: <a href="https://rainviewer.com" target="_blank">RainViewer</a>',maxZoom:19,zIndex:5});radarLayer.addTo(map);}}
function fetchLatestRadar(cb){
  fetch('/radar')
    .then(function(r){if(!r.ok)throw new Error('HTTP '+r.status);return r.json();})
    .then(function(data){
      if(data.error){cb(data.error,null,null);}
      else{cb(null,data.host,data.path);}
    }).catch(function(e){cb(String(e),null,null);});}
function refreshRadar(){
  fetchLatestRadar(function(err,host,path){
    if(err){console.warn('[radar]',err);return;}
    applyRadarLayer(host,path);});}
function enableRadar(){
  radarEnabled=true;
  var btn=document.getElementById('radar-btn');
  btn.className='on';btn.textContent='◉ RADAR ON';
  fetchLatestRadar(function(err,host,path){
    if(err){
      console.warn('[radar]',err);
      radarEnabled=false;btn.className='off';btn.textContent='◎ RADAR ERR';
      setTimeout(function(){btn.textContent='◎ RADAR OFF';},3000);return;}
    applyRadarLayer(host,path);
    radarRefreshTimer=setInterval(refreshRadar,300000);});}
function disableRadar(){
  radarEnabled=false;
  if(radarRefreshTimer){clearInterval(radarRefreshTimer);radarRefreshTimer=null;}
  if(radarLayer){map.removeLayer(radarLayer);radarLayer=null;}
  var btn=document.getElementById('radar-btn');btn.className='off';btn.textContent='◎ RADAR OFF';}
var markers=[],beamLines=[],_tunedSpot=null,_tunedCardSpot=null,_activatorMode=false,_lastPark='',_flrigFreqKhz=null,_flrigMode=null,_pinnedTuned=null;
var BAND_COLORS={'160m':'#ff4444','80m':'#ff8800','60m':'#ffcc00','40m':'#aaff00',
  '30m':'#00ffaa','20m':'#00e5ff','17m':'#0088ff','15m':'#8844ff',
  '12m':'#ff44cc','10m':'#ff2288','6m':'#ff0055','2m':'#ff6688','other':'#aaaaaa'};
var BAND_COLORS_DAY={'160m':'#cc0000','80m':'#b85a00','60m':'#907000','40m':'#3a7000',
  '30m':'#006a40','20m':'#005899','17m':'#0055cc','15m':'#5500cc',
  '12m':'#aa0099','10m':'#cc0066','6m':'#cc0033','2m':'#993344','other':'#555555'};
function freqToBand(k){
  if(!k)return 'other';
  if(k<2000)return '160m';if(k<4000)return '80m';if(k<5500)return '60m';
  if(k<8000)return '40m';if(k<11000)return '30m';if(k<15500)return '20m';
  if(k<18500)return '17m';if(k<22000)return '15m';if(k<25000)return '12m';
  if(k<30000)return '10m';if(k<54000)return '6m';if(k<148000)return '2m';
  return 'other';}
function clearMarkers(){
  markers.forEach(function(m){map.removeLayer(m);});markers=[];
  beamLines.forEach(function(l){map.removeLayer(l);});beamLines=[];}
function showTunedCard(s){
  var tsBox=document.getElementById('tuned-station-box');
  var tsEmpty=document.getElementById('ts-empty');
  if(s&&s.activator){
    _tunedCardSpot=s;
    document.getElementById('ts-call').textContent=s.activator;
    var tdet='';
    if(s.gs)tdet+='<span>Grid: '+s.gs+'</span>';
    if(s.park)tdet+='<span>Park: '+s.park+(s.park_name?' — '+s.park_name:'')+'</span>';
    var mhz=s.freq_khz?(s.freq_khz/1000).toFixed(3)+' MHz':'';
    if(mhz||s.mode)tdet+='<span>'+[mhz,s.mode].filter(Boolean).join(' ')+'</span>';
    document.getElementById('ts-detail').innerHTML=tdet;
    tsBox.style.display='block';tsEmpty.style.display='none';
  }else{tsBox.style.display='none';tsEmpty.style.display='block';}}
function updateStatsPanel(d){
  var spots=d.spots||[],qsos=d.qsos||[];
  document.getElementById('stat-spots').textContent=spots.length||'—';
  document.getElementById('stat-qsos').textContent=qsos.length||'—';
  var bc={};
  spots.forEach(function(s){var b=freqToBand(s.freq_khz);bc[b]=(bc[b]||0)+1;});
  var keys=Object.keys(bc),bandsEl=document.getElementById('stat-bands');
  if(!keys.length){bandsEl.innerHTML='<div style="color:var(--dim);font-size:.6rem;letter-spacing:2px">NO SPOTS</div>';}
  else{bandsEl.innerHTML=keys.sort().map(function(b){
    var c=(_dayMode?BAND_COLORS_DAY:BAND_COLORS)[b]||(_dayMode?'#555':'#aaa');
    return '<div class="chip" style="border-color:'+c+';color:'+c+'"><strong>'+bc[b]+'</strong> '+b+'</div>';
  }).join('');}
  var llBox=document.getElementById('last-logged-box');
  var llEmpty=document.getElementById('ll-empty');
  if(d.last_qso){
    var lq=d.last_qso;
    document.getElementById('ll-call').textContent=lq.call;
    var det='';
    if(lq.gs)det+='<span>Grid: '+lq.gs+'</span>';
    if(lq.park)det+='<span>Park: '+lq.park+(lq.park_name?' — '+lq.park_name:'')+'</span>';
    if(lq.band||lq.mode)det+='<span>'+(lq.band||'')+' '+(lq.mode||'')+'</span>';
    document.getElementById('ll-detail').innerHTML=det;
    llBox.style.display='block';llEmpty.style.display='none';
  }else{llBox.style.display='none';llEmpty.style.display='block';}
  if(d.tuned_spots&&d.tuned_spots.length&&d.tuned_spots[0].activator){
    var preferred=null;
    if(_pinnedTuned){
      preferred=d.tuned_spots.find(function(t){return t.activator===_pinnedTuned.activator&&t.park===_pinnedTuned.park;})||null;
      if(!preferred)_pinnedTuned=null;
    }
    _tunedSpot=preferred||d.tuned_spots[0];
    showTunedCard(_tunedSpot);
  }else{_tunedSpot=null;_pinnedTuned=null;showTunedCard(null);}}
function updateSpotsPanel(d){
  var spots=(d.spots||[]).slice();
  spots.sort(function(a,b){return(a.spot_time||'').localeCompare(b.spot_time||'');});
  var el=document.getElementById('spots-list');
  if(!spots.length){el.innerHTML='<div class="no-spots">NO ACTIVE SPOTS<br><br>Enable POTA scan to<br>populate this panel</div>';return;}
  el.innerHTML=spots.map(function(s,i){
    var cls=s.tuned?'tuned':s.worked?'worked':'';
    var badge=s.tuned?'<span class="spot-badge tuned">&#9679; TUNED</span>'
      :s.worked?'<span class="spot-badge worked">&#10003; WORKED</span>':'';
    var mhz=s.freq_khz?(s.freq_khz/1000).toFixed(3)+' MHz':'?';
    var band=freqToBand(s.freq_khz),bc=(_dayMode?BAND_COLORS_DAY:BAND_COLORS)[band]||(_dayMode?'#555':'#aaa');
    return '<div class="spot-item '+cls+'" data-i="'+i+'">'
      +'<div class="spot-call"><span>'+s.activator+'</span>'+badge+'</div>'
      +'<div class="spot-meta"><span class="spot-park">'+(s.park||'?')+'</span>'
      +'<span style="color:'+bc+'">'+band+'</span>'
      +'<span>'+mhz+'</span><span>'+(s.mode||'')+'</span></div>'
      +'<div class="spot-dbl-hint">DBL-CLICK TO LOG QSO</div></div>';
  }).join('');
  el.querySelectorAll('.spot-item').forEach(function(item){
    var i=parseInt(item.dataset.i);
    var s=spots[i];
    item.addEventListener('click',function(){
      _pinnedTuned={activator:s.activator,park:s.park};
      showTunedCard(s);
      fetch('/tune',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({activator:s.activator,park:s.park,freq_khz:s.freq_khz,mode:s.mode,tuned:s.tuned})})
        .then(function(r){return r.json();}).then(function(d){
          if(d.out_of_band){var ov=document.getElementById('oob-overlay');
            ov.style.display='flex';setTimeout(function(){ov.style.display='none';},4000);}});
    });
    item.addEventListener('dblclick',function(e){
      e.stopPropagation();
      openLogModal(s);
    });});}
function refreshData(){
  fetch('/data').then(function(r){return r.json();}).then(function(d){
    clearMarkers();
    (d.qsos||[]).forEach(function(q){
      var m=L.circleMarker([q.lat,q.lon],{radius:5,color:'#cc44ff',fillColor:'#cc44ff',fillOpacity:0.7,weight:1});
      var pop='<b>'+q.call+'</b>';
      if(q.park)pop+=' ['+q.park+']';
      if(q.band||q.mode)pop+='<br>'+[q.band,q.mode].filter(Boolean).join(' ');
      if(q.date)pop+='<br>'+q.date+' '+q.time_on+'z';
      m.bindPopup(pop);m.addTo(map);markers.push(m);});
    (d.spots||[]).forEach(function(s){
      var color=s.tuned?'#00e5ff':s.worked?'#00bb44':(_dayMode?'#000000':'#ffff00');
      var r=s.tuned?7:5;
      var cls=(!s.tuned&&!s.worked)?'spot-flash':'';
      var m=L.circleMarker([s.lat,s.lon],{radius:r,color:color,fillColor:color,fillOpacity:0.85,weight:s.tuned?2:1,className:cls});
      var pop=s.activator+' ['+s.park+']<br>'+s.freq_khz+' kHz '+s.mode;
      if(s.tuned)pop+='<br><b>&#x25CF; TUNED</b>';
      if(s.worked)pop+='<br><b>Worked</b>';
      if(s.tuned)pop+='<br><button onclick="openLogModal(_tunedSpot)" style="margin-top:4px;font-size:.55rem;letter-spacing:2px;padding:3px 10px;border:1px solid #00e5ff;color:#00e5ff;background:transparent;cursor:pointer;">&#9998; LOG QSO</button>';
      m.bindPopup(pop);
      m.on('click',function(e){
        L.DomEvent.stopPropagation(e);
        fetch('/tune',{method:'POST',headers:{'Content-Type':'application/json'},
          body:JSON.stringify({activator:s.activator,park:s.park,freq_khz:s.freq_khz,mode:s.mode,tuned:s.tuned})})
          .then(function(r){return r.json();}).then(function(d){
            if(d.out_of_band){var ov=document.getElementById('oob-overlay');
              ov.style.display='flex';setTimeout(function(){ov.style.display='none';},4000);}});});
      m.addTo(map);markers.push(m);});
    if(d.my_grid){
      var mg=d.my_grid;
      var star=L.marker([mg.lat,mg.lon],{icon:L.divIcon({
        html:'<span style="color:#ff2222;font-size:18px;">&#9733;</span>',
        className:'',iconAnchor:[9,9]})});
      star.bindPopup('My grid: '+mg.gs);star.addTo(map);markers.push(star);}
    if(d.my_grid&&d.tuned_spots&&d.tuned_spots.length){
      d.tuned_spots.forEach(function(ts){
        var gcp=gcPoints(d.my_grid.lat,d.my_grid.lon,ts.lat,ts.lon,60);
        var bl=L.polyline(gcp,{color:'#00e5ff',weight:2.5,dashArray:'12 8',opacity:0.85,className:'beam-anim'});
        bl.addTo(map);beamLines.push(bl);});}
    updateStatsPanel(d);
    updateSpotsPanel(d);
    var sb=document.getElementById('scan-btn');
    if(d.scanning){sb.className='active';sb.textContent='▶ SCANNING';}
    else{sb.className='paused';sb.textContent='⏸ SCAN PAUSED';}
    document.getElementById('scan-overlay').style.display=d.scanning?'block':'none';
    if(d.callsign){document.getElementById('mycall').textContent=d.callsign;}
    var dot=document.getElementById('status-dot');
    var stxt=document.getElementById('status-text');
    if(d.flrig_connected){dot.className='status-dot connected';stxt.textContent='CONNECTED';}
    else{dot.className='status-dot offline';stxt.textContent='OFFLINE';}
    if(d.flrig_freq_khz!=null){_flrigFreqKhz=d.flrig_freq_khz;}
    if(d.flrig_mode){_flrigMode=d.flrig_mode;}
    var vfoEl=document.getElementById('vfo-display');
    if(vfoEl){
      if(d.flrig_connected&&d.flrig_freq_khz!=null){
        var mhz=(d.flrig_freq_khz/1000).toFixed(4);
        vfoEl.textContent=mhz+' MHz \xb7 '+(d.flrig_mode||'—');
        vfoEl.className='live';
      }else{vfoEl.textContent='— \xb7 —';vfoEl.className='';}}
  }).catch(function(e){console.error('Fetch error:',e);});}
function gcPoints(la1,lo1,la2,lo2,n){
  var R=Math.PI/180;
  var f1=la1*R,l1=lo1*R,f2=la2*R,l2=lo2*R;
  var d=2*Math.asin(Math.sqrt(Math.pow(Math.sin((f2-f1)/2),2)+Math.cos(f1)*Math.cos(f2)*Math.pow(Math.sin((l2-l1)/2),2)));
  if(d<1e-6)return[[la1,lo1],[la2,lo2]];
  var pts=[];
  for(var i=0;i<=n;i++){
    var f=i/n,A=Math.sin((1-f)*d)/Math.sin(d),B=Math.sin(f*d)/Math.sin(d);
    var x=A*Math.cos(f1)*Math.cos(l1)+B*Math.cos(f2)*Math.cos(l2);
    var y=A*Math.cos(f1)*Math.sin(l1)+B*Math.cos(f2)*Math.sin(l2);
    var z=A*Math.sin(f1)+B*Math.sin(f2);
    pts.push([Math.atan2(z,Math.sqrt(x*x+y*y))/R,Math.atan2(y,x)/R]);}
  return pts;}
function updateClock(){
  var n=new Date();
  document.getElementById('clock').textContent=
    ('0'+n.getUTCHours()).slice(-2)+':'+('0'+n.getUTCMinutes()).slice(-2)+':'+('0'+n.getUTCSeconds()).slice(-2)+' ZULU';}
setInterval(updateClock,1000);updateClock();
var _dotPhase=0;
setInterval(function(){
  _dotPhase=(_dotPhase+1)%3;
  var el=document.getElementById('scan-dots');
  if(el)el.textContent='.'.repeat(_dotPhase+1);
},500);
function toggleMode(){
  _activatorMode=!_activatorMode;
  var btn=document.getElementById('mode-toggle-btn');
  if(_activatorMode){
    btn.textContent='HUNTER MODE';btn.className='mode-btn activator';_lastPark='';openLogModal(null);
    var banner=document.getElementById('activator-banner');
    banner.classList.remove('banner-flashing');
    void banner.offsetWidth;
    banner.classList.add('banner-flashing');
    banner.addEventListener('animationend',function(){banner.classList.remove('banner-flashing');},{once:true});}
  else{btn.textContent='ACTIVATOR MODE';btn.className='mode-btn';}}
function toggleRespoPanel(){
  var p=document.getElementById('respot-panel');
  var open=p.style.display==='none'||p.style.display==='';
  p.style.display=open?'block':'none';
  if(open){
    document.getElementById('respot-park').value=_lastPark;
    document.getElementById('respot-freq').value=_flrigFreqKhz?String(_flrigFreqKhz):'';
    document.getElementById('respot-mode').value=_flrigMode||'';
    document.getElementById('respot-comment').value='';
    document.getElementById('respot-status').textContent='';
    document.getElementById('respot-status').className='';
    document.getElementById('respot-go-btn').disabled=false;
    setTimeout(function(){document.getElementById('respot-park').focus();},50);}}
function doRespot(){
  var park=document.getElementById('respot-park').value.trim().toUpperCase();
  var st=document.getElementById('respot-status');
  if(!park){st.textContent='PARK REF REQUIRED';st.className='err';return;}
  var freq=document.getElementById('respot-freq').value.trim();
  var mode=document.getElementById('respot-mode').value.trim().toUpperCase();
  var comment=document.getElementById('respot-comment').value.trim();
  var btn=document.getElementById('respot-go-btn');
  btn.disabled=true;st.textContent='SPOTTING…';st.className='';
  var payload={reference:park};
  if(freq)payload.freq_khz=freq;
  if(mode)payload.mode=mode;
  if(comment)payload.comment=comment;
  fetch('/respot-self',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)})
    .then(function(r){return r.json();})
    .then(function(data){
      if(data.ok){
        _lastPark=park;
        st.textContent='SPOT SENT ✔';st.className='ok';
        setTimeout(function(){st.textContent='';st.className='';btn.disabled=false;},3000);
      }else{st.textContent='ERROR: '+(data.error||'unknown');st.className='err';btn.disabled=false;}})
    .catch(function(){st.textContent='NETWORK ERROR';st.className='err';btn.disabled=false;});}
function submitSelfSpot(){
  var park=document.getElementById('lm-park').value.trim().toUpperCase();
  var st=document.getElementById('lm-status');
  if(!park){st.textContent='PARK REF REQUIRED';st.className='err';return;}
  var freq=document.getElementById('lm-freq').value.trim();
  var mode=document.getElementById('lm-mode').value.trim().toUpperCase();
  var comment=document.getElementById('lm-comment').value.trim();
  var btn=document.getElementById('lm-selfspot-btn');
  btn.disabled=true;st.textContent='SPOTTING…';st.className='';
  var payload={reference:park};
  if(freq)payload.freq_khz=freq;
  if(mode)payload.mode=mode;
  if(comment)payload.comment=comment;
  fetch('/respot-self',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)})
  .then(function(r){return r.json();})
  .then(function(data){
    if(data.ok){st.textContent='SELF SPOT SENT ✔';st.className='ok';
      setTimeout(function(){st.textContent='';st.className='';btn.disabled=false;},2500);}
    else{st.textContent='SPOT ERROR: '+(data.error||'unknown');st.className='err';btn.disabled=false;}})
  .catch(function(){st.textContent='NETWORK ERROR';st.className='err';btn.disabled=false;});}
function openLogModal(spot){
  var modal=document.getElementById('log-modal');
  if(_activatorMode){modal.classList.add('activator-mode');}
  else{modal.classList.remove('activator-mode');}
  document.getElementById('lm-selfspot-btn').style.display=_activatorMode?'':'none';
  var freqEl=document.getElementById('lm-freq');
  var modeEl=document.getElementById('lm-mode');
  document.getElementById('lm-name').value='';
  document.getElementById('lm-qth').value='';
  if(_activatorMode){
    document.getElementById('lm-call').value='';
    document.getElementById('lm-park').value=_lastPark||'';
    document.getElementById('lm-grid').value='';
    document.getElementById('lm-comment').value='';
    freqEl.value=_flrigFreqKhz?String(_flrigFreqKhz):'';
    modeEl.value=_flrigMode||'';
    freqEl.removeAttribute('readonly');
    modeEl.removeAttribute('readonly');
  }else{
    var s=spot||_tunedSpot;
    if(!s)return;
    var preCall=s.activator||'';
    document.getElementById('lm-call').value=preCall;
    document.getElementById('lm-park').value=s.park||'';
    freqEl.value=s.freq_khz?String(s.freq_khz):'';
    modeEl.value=s.mode||'';
    document.getElementById('lm-grid').value=s.gs||'';
    freqEl.setAttribute('readonly','');
    modeEl.setAttribute('readonly','');
    if(preCall){lmLookupCall(preCall);}
  }
  document.getElementById('lm-rst-s').value='59';
  document.getElementById('lm-rst-r').value='59';
  document.getElementById('lm-comment').value='';
  var st=document.getElementById('lm-status');st.textContent='';st.className='';
  document.getElementById('lm-submit-btn').disabled=false;
  document.getElementById('lm-clear-btn').style.display=_activatorMode?'':'none';
  document.getElementById('lm-multiop-btn').style.display=_activatorMode?'none':'';
  document.getElementById('activator-banner').style.display=_activatorMode?'block':'none';
  fetch('/scan_pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:1})});
  var ov=document.getElementById('log-modal-overlay');ov.style.display='flex';
  setTimeout(function(){document.getElementById('lm-call').focus();},50);}
function closeLogModal(){
  if(_activatorMode){deactivateActivatorMode();return;}
  document.getElementById('lm-freq').setAttribute('readonly','');
  document.getElementById('lm-mode').setAttribute('readonly','');
  document.getElementById('log-modal-overlay').style.display='none';
  fetch('/scan_pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:0})});}
function deactivateActivatorMode(){
  _activatorMode=false;
  document.getElementById('mode-toggle-btn').textContent='ACTIVATOR MODE';
  document.getElementById('mode-toggle-btn').className='mode-btn';
  document.getElementById('lm-clear-btn').style.display='none';
  document.getElementById('activator-banner').style.display='none';
  document.getElementById('lm-freq').setAttribute('readonly','');
  document.getElementById('lm-mode').setAttribute('readonly','');
  document.getElementById('log-modal-overlay').style.display='none';
  fetch('/scan_pause',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({state:0})});}
function submitLogQSO(){
  var call=document.getElementById('lm-call').value.trim().toUpperCase();
  var st=document.getElementById('lm-status');
  if(!call){st.textContent='CALLSIGN REQUIRED';st.className='err';return;}
  var payload={
    call:call,
    name:document.getElementById('lm-name').value.trim(),
    qth:document.getElementById('lm-qth').value.trim(),
    park:document.getElementById('lm-park').value.trim(),
    freq_khz:parseFloat(document.getElementById('lm-freq').value)||0,
    mode:document.getElementById('lm-mode').value.trim().toUpperCase(),
    rst_sent:document.getElementById('lm-rst-s').value.trim()||'59',
    rst_rcvd:document.getElementById('lm-rst-r').value.trim()||'59',
    gridsquare:document.getElementById('lm-grid').value.trim().toUpperCase(),
    comment:document.getElementById('lm-comment').value.trim()};
  var btn=document.getElementById('lm-submit-btn');
  btn.disabled=true;st.textContent='LOGGING…';st.className='';
  fetch('/log',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)})
    .then(function(r){return r.json();})
    .then(function(data){
      if(data.ok){
        _lastPark=payload.park||_lastPark;
        st.textContent='LOGGED ✔';st.className='ok';
        if(_activatorMode){
          setTimeout(function(){
            document.getElementById('lm-call').value='';
            document.getElementById('lm-name').value='';
            document.getElementById('lm-qth').value='';
            document.getElementById('lm-park').value=_lastPark||'';
            document.getElementById('lm-rst-s').value='59';
            document.getElementById('lm-rst-r').value='59';
            st.textContent='';st.className='';
            btn.disabled=false;
            document.getElementById('lm-call').focus();
          },700);
        }else{setTimeout(closeLogModal,900);}
      }else{st.textContent='ERROR: '+(data.error||'unknown');st.className='err';btn.disabled=false;}})
    .catch(function(){st.textContent='NETWORK ERROR';st.className='err';btn.disabled=false;});}
function submitMultiOp(){
  var call=document.getElementById('lm-call').value.trim().toUpperCase();
  var st=document.getElementById('lm-status');
  if(!call){st.textContent='CALLSIGN REQUIRED';st.className='err';return;}
  var payload={
    call:call,
    name:document.getElementById('lm-name').value.trim(),
    qth:document.getElementById('lm-qth').value.trim(),
    park:document.getElementById('lm-park').value.trim(),
    freq_khz:parseFloat(document.getElementById('lm-freq').value)||0,
    mode:document.getElementById('lm-mode').value.trim().toUpperCase(),
    rst_sent:document.getElementById('lm-rst-s').value.trim()||'59',
    rst_rcvd:document.getElementById('lm-rst-r').value.trim()||'59',
    gridsquare:document.getElementById('lm-grid').value.trim().toUpperCase(),
    comment:document.getElementById('lm-comment').value.trim()};
  var btn=document.getElementById('lm-multiop-btn');
  btn.disabled=true;st.textContent='LOGGING…';st.className='';
  fetch('/log',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify(payload)})
    .then(function(r){return r.json();})
    .then(function(data){
      if(data.ok){
        st.textContent='LOGGED ✔';st.className='ok';
        setTimeout(function(){
          document.getElementById('lm-call').value='';
          document.getElementById('lm-name').value='';
          document.getElementById('lm-qth').value='';
          document.getElementById('lm-rst-s').value='59';
          document.getElementById('lm-rst-r').value='59';
          st.textContent='';st.className='';
          btn.disabled=false;
          document.getElementById('lm-call').focus();
        },700);
      }else{st.textContent='ERROR: '+(data.error||'unknown');st.className='err';btn.disabled=false;}})
    .catch(function(){st.textContent='NETWORK ERROR';st.className='err';btn.disabled=false;});}
refreshData();
setInterval(refreshData,2000);
map.on('click',function(){fetch('/scan',{method:'POST'});});
document.getElementById('scan-btn').addEventListener('click',function(e){e.stopPropagation();fetch('/scan',{method:'POST'});});
document.getElementById('radar-btn').addEventListener('click',function(e){e.stopPropagation();if(radarEnabled){disableRadar();}else{enableRadar();}});
document.addEventListener('keydown',function(e){if(e.key==='Escape'){closeLogModal();}});
document.addEventListener('click',function(e){if(e.target.id==='log-modal-overlay'){closeLogModal();}});
function lmLookupCall(call){
  if(!call)return;
  fetch('/lookup?call='+encodeURIComponent(call))
    .then(function(r){return r.json();})
    .then(function(d){
      var ne=document.getElementById('lm-name');
      var qe=document.getElementById('lm-qth');
      if(d.name&&!ne.value.trim())ne.value=d.name;
      if(d.qth&&!qe.value.trim())qe.value=d.qth;
    }).catch(function(){});}
var _lmCallDebounce=null;
document.addEventListener('focusout',function(e){
  if(e.target.id!=='lm-call')return;
  lmLookupCall(e.target.value.trim().toUpperCase());});
document.addEventListener('input',function(e){
  if(e.target.id!=='lm-call')return;
  if(!_activatorMode)return;
  var v=e.target.value.trim().toUpperCase();
  clearTimeout(_lmCallDebounce);
  if(v.length>=3){_lmCallDebounce=setTimeout(function(){lmLookupCall(v);},400);}});
function clearLogModal(){
  document.getElementById('lm-call').value='';
  document.getElementById('lm-name').value='';
  document.getElementById('lm-qth').value='';
  document.getElementById('lm-rst-s').value='59';
  document.getElementById('lm-rst-r').value='59';
  document.getElementById('lm-comment').value='';
  var st=document.getElementById('lm-status');st.textContent='';st.className='';
  document.getElementById('lm-call').focus();}
</script>
<div id="log-modal-overlay">
  <div id="activator-banner">&#9632; ACTIVATED &#9632;</div>
  <div id="log-modal">
    <div class="lm-title">&#9632; LOG QSO</div>
    <div class="lm-row">
      <div class="lm-label">Callsign</div>
      <input class="lm-input" id="lm-call" type="text" autocomplete="off" spellcheck="false">
    </div>
    <div class="lm-row">
      <div class="lm-label">Name</div>
      <input class="lm-input" id="lm-name" type="text" autocomplete="off" placeholder="Auto-filled">
    </div>
    <div class="lm-row">
      <div class="lm-label">City / State</div>
      <input class="lm-input" id="lm-qth" type="text" autocomplete="off" placeholder="Auto-filled">
    </div>
    <div class="lm-row">
      <div class="lm-label">Park Reference</div>
      <input class="lm-input" id="lm-park" type="text" autocomplete="off" spellcheck="false">
    </div>
    <div class="lm-row-2">
      <div class="lm-row" style="margin-bottom:0">
        <div class="lm-label">Freq (kHz)</div>
        <input class="lm-input" id="lm-freq" type="text" readonly>
      </div>
      <div class="lm-row" style="margin-bottom:0">
        <div class="lm-label">Mode</div>
        <input class="lm-input" id="lm-mode" type="text" readonly>
      </div>
    </div>
    <div class="lm-row-2" style="margin-top:11px">
      <div class="lm-row" style="margin-bottom:0">
        <div class="lm-label">RST Sent</div>
        <input class="lm-input" id="lm-rst-s" type="text" value="59">
      </div>
      <div class="lm-row" style="margin-bottom:0">
        <div class="lm-label">RST Rcvd</div>
        <input class="lm-input" id="lm-rst-r" type="text" value="59">
      </div>
    </div>
    <div class="lm-row" style="margin-top:11px">
      <div class="lm-label">Grid Square</div>
      <input class="lm-input" id="lm-grid" type="text" autocomplete="off">
    </div>
    <div class="lm-row">
      <div class="lm-label">Comment</div>
      <input class="lm-input" id="lm-comment" type="text" autocomplete="off">
    </div>
    <div id="lm-status"></div>
    <div class="lm-btns" style="margin-top:16px">
      <button class="lm-btn confirm" id="lm-submit-btn" onclick="submitLogQSO()">&#9632; LOG QSO</button>
    </div>
    <div class="lm-btns" style="margin-top:8px">
      <button class="lm-btn multiop" id="lm-multiop-btn" onclick="submitMultiOp()">&#9654; MULTI-OP</button>
    </div>
    <div class="lm-btns" style="margin-top:8px">
      <button class="lm-btn selfspot" id="lm-selfspot-btn" style="display:none" onclick="submitSelfSpot()">&#10023; SELF SPOT</button>
      <button class="lm-btn clear" id="lm-clear-btn" style="display:none" onclick="clearLogModal()">&#9249; CLEAR</button>
      <button class="lm-btn cancel" onclick="closeLogModal()">&#10005; CANCEL</button>
    </div>
  </div>
</div>
</body>
</html>"""

        class _Handler(http.server.BaseHTTPRequestHandler):
            def _send_json(self, obj, status=200):
                body = json.dumps(obj).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                parsed = urllib.parse.urlparse(self.path)
                params = urllib.parse.parse_qs(parsed.query)
                if parsed.path == '/data':
                    self._handle_data()
                elif parsed.path == '/debug':
                    self._handle_debug()
                elif parsed.path == '/radar':
                    self._handle_radar()
                elif parsed.path == '/lookup':
                    call = params.get("call", [""])[0].strip().upper()
                    info = {}
                    if call:
                        result = (fcc_lookup(call) or hamdb_lookup(call)
                                  or qrz_lookup(call))
                        if result:
                            info = result
                    self._send_json(info)
                else:
                    body = MAP_HTML.encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store, no-cache, must-revalidate")
                    self.send_header("Pragma", "no-cache")
                    self.end_headers()
                    self.wfile.write(body)

            def _handle_debug(self):
                filtered = app._pota_spots_filtered or []
                refs = list({s.get("reference", s.get("parkReference", ""))
                             for s in filtered})
                db_exists = os.path.exists(PARKS_DB)
                db_count = 0
                db_grid_count = 0
                db_error = None
                ref_to_grid = {}
                if db_exists and refs:
                    try:
                        placeholders = ",".join("?" * len(refs))
                        with sqlite3.connect(PARKS_DB) as _pk:
                            db_count = _pk.execute(
                                "SELECT COUNT(*) FROM parks").fetchone()[0]
                            db_grid_count = _pk.execute(
                                "SELECT COUNT(*) FROM parks WHERE grid != ''").fetchone()[0]
                            park_rows = _pk.execute(
                                f"SELECT reference, grid FROM parks "
                                f"WHERE reference IN ({placeholders})",
                                refs).fetchall()
                        ref_to_grid = {r[0]: r[1] for r in park_rows if r[1]}
                    except Exception as e:
                        db_error = str(e)
                sample = filtered[:3] if filtered else []
                self._send_json({
                    "spots_raw_count": len(app._pota_spots_raw),
                    "spots_filtered_count": len(filtered),
                    "sample_spot_keys": list(sample[0].keys()) if sample else [],
                    "sample_refs": refs[:5],
                    "parks_db_exists": db_exists,
                    "parks_db_total": db_count,
                    "parks_db_with_grid": db_grid_count,
                    "refs_matched_in_db": len(ref_to_grid),
                    "sample_matches": dict(list(ref_to_grid.items())[:5]),
                    "db_error": db_error,
                    "my_grid": app.cfg.get("gridsquare"),
                })

            def _handle_radar(self):
                try:
                    req = urllib.request.Request(
                        'https://api.rainviewer.com/public/weather-maps.json',
                        headers={'User-Agent': 'Mozilla/5.0'})
                    with urllib.request.urlopen(req, timeout=8) as resp:
                        data = json.loads(resp.read().decode())
                    host = data.get('host', 'https://tilecache.rainviewer.com')
                    past = (data.get('radar') or {}).get('past') or []
                    if past:
                        latest = past[-1]
                        self._send_json({'host': host, 'path': latest['path']})
                    else:
                        self._send_json({'error': 'no radar frames'}, 500)
                except Exception as e:
                    self._send_json({'error': str(e)}, 500)

            def _handle_data(self):
                try:
                    worked_calls = set()
                    try:
                        worked_calls = {r[0] for r in app.conn.execute(
                            "SELECT DISTINCT UPPER(TRIM(call)) FROM qso").fetchall() if r[0]}
                    except Exception:
                        pass
                    if (time.monotonic() < app._tune_suppress_until
                            and app._pota_clicked_hz):
                        vfo_hz = app._pota_clicked_hz
                    else:
                        vfo_hz = (app._flrig_freq_hz if app._flrig_freq_hz is not None
                                  else app._pota_clicked_hz)
                    # The POTA API includes grid4/grid6 and lat/lon directly
                    # on each spot — no parks DB lookup needed.
                    filtered = app._pota_spots_filtered or []
                    spots_out = []
                    for s in filtered:
                        park = str(s.get("reference", s.get("parkReference", ""))).strip()
                        gs = (s.get("grid6") or s.get("grid4") or "")[:6].strip().upper()
                        # Prefer API-supplied lat/lon; fall back to grid conversion
                        try:
                            lat = float(s["latitude"])
                            lon = float(s["longitude"])
                        except (KeyError, TypeError, ValueError):
                            if len(gs) >= 4:
                                lat, lon = grid_to_latlon(gs)
                            else:
                                lat, lon = None, None
                        if lat is None or lon is None:
                            continue
                        activator = str(s.get("activator",
                                               s.get("activatorCallsign", ""))).strip().upper()
                        try:
                            freq_khz = float(s.get("frequency", s.get("freq", 0)))
                            spot_hz = int(freq_khz * 1000)
                        except (ValueError, TypeError):
                            freq_khz = 0
                            spot_hz = 0
                        tuned = bool(vfo_hz is not None and spot_hz
                                     and spot_hz == int(vfo_hz))
                        worked = activator in worked_calls
                        mode = str(s.get("mode", "")).strip()
                        park_name = str(s.get("name", s.get("parkName", ""))).strip()
                        spots_out.append({
                            "gs": gs, "activator": activator, "park": park,
                            "park_name": park_name, "freq_khz": freq_khz, "mode": mode,
                            "tuned": tuned, "worked": worked,
                            "lat": lat, "lon": lon,
                            "spot_time": str(s.get("spotTime", s.get("timestamp", ""))),
                        })
                    my_grid_data = None
                    tuned_spots = []
                    my_gs = (app.cfg.get("gridsquare") or "")[:6].strip().upper()
                    if len(my_gs) >= 4:
                        mlat, mlon = grid_to_latlon(my_gs)
                        if mlat is not None:
                            my_grid_data = {"gs": my_gs, "lat": mlat, "lon": mlon}
                    for sp in spots_out:
                        if sp["tuned"]:
                            tuned_spots.append({
                                "lat": sp["lat"], "lon": sp["lon"],
                                "activator": sp["activator"], "park": sp["park"],
                                "park_name": sp["park_name"], "gs": sp["gs"],
                                "freq_khz": sp["freq_khz"], "mode": sp["mode"],
                            })
                    # Build QSO markers from logged contacts
                    qsos_out = []
                    try:
                        qso_rows = app.conn.execute(
                            "SELECT call, gridsquare, park_nr, date, time_on, band, mode "
                            "FROM qso ORDER BY date DESC, time_on DESC"
                        ).fetchall()
                        # Pre-fetch park lat/lon and name for any park_nr references
                        park_latlon = {}
                        park_names = {}
                        park_nrs = list({r[2] for r in qso_rows if r[2]})
                        if park_nrs and os.path.exists(PARKS_DB):
                            try:
                                with sqlite3.connect(PARKS_DB) as _pk:
                                    placeholders = ",".join("?" * len(park_nrs))
                                    rows_pk = _pk.execute(
                                        f"SELECT reference, latitude, longitude, grid, name "
                                        f"FROM parks WHERE reference IN ({placeholders})",
                                        park_nrs
                                    ).fetchall()
                                for ref, plat, plon, pgrid, pname in rows_pk:
                                    park_names[ref] = pname or ""
                                    try:
                                        park_latlon[ref] = (float(plat), float(plon))
                                    except (TypeError, ValueError):
                                        if pgrid and len(pgrid) >= 4:
                                            park_latlon[ref] = grid_to_latlon(pgrid)
                            except Exception:
                                pass
                        seen = set()
                        for r in qso_rows:
                            call, gs, park_nr, date, time_on, band, mode = r
                            call = (call or "").upper().strip()
                            if not call:
                                continue
                            key = (call, gs or "", park_nr or "")
                            if key in seen:
                                continue
                            seen.add(key)
                            lat, lon = None, None
                            if gs and len(gs) >= 4:
                                lat, lon = grid_to_latlon(gs)
                            if (lat is None or lon is None) and park_nr and park_nr in park_latlon:
                                lat, lon = park_latlon[park_nr]
                            if lat is None or lon is None:
                                continue
                            qsos_out.append({
                                "call": call, "park": park_nr or "",
                                "park_name": park_names.get(park_nr, "") if park_nr else "",
                                "gs": gs or "", "date": date or "",
                                "time_on": time_on or "", "band": band or "",
                                "mode": mode or "", "lat": lat, "lon": lon,
                            })
                    except Exception:
                        pass
                    self._send_json({
                        "spots": spots_out,
                        "qsos": qsos_out,
                        "my_grid": my_grid_data,
                        "tuned_spots": tuned_spots,
                        "scanning": app._pota_scan_active,
                        "callsign": app.cfg.get("callsign", ""),
                        "flrig_connected": app._flrig_freq_hz is not None,
                        "flrig_freq_khz": round(float(app._flrig_freq_hz) / 1000, 3) if app._flrig_freq_hz else None,
                        "flrig_mode": str(app._flrig_mode).upper() if app._flrig_mode else None,
                        "last_qso": qsos_out[0] if qsos_out else None,
                    })
                except Exception as exc:
                    self._send_json({"error": str(exc)}, status=500)

            def do_POST(self):
                if self.path == '/tune':
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                        data = json.loads(self.rfile.read(length))
                    except Exception:
                        self._send_json({"error": "bad json"}, status=400)
                        return
                    try:
                        freq_mhz = float(data.get("freq_khz", 0)) / 1000.0
                    except (TypeError, ValueError):
                        freq_mhz = 0.0
                    lic = app.cfg.get("license_class", "Extra")
                    oob = bool(freq_mhz) and not _in_band(freq_mhz, lic, str(data.get("mode","")))
                    app.after(0, lambda d=data: app._on_map_station_click(d))
                    if oob:
                        app.after(0, lambda f=freq_mhz: app._warn_out_of_band(f))
                    self._send_json({"ok": True, "out_of_band": oob})
                elif self.path == '/respot-self':
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                        data = json.loads(self.rfile.read(length))
                    except Exception:
                        self._send_json({"error": "bad json"}, status=400)
                        return
                    reference = str(data.get("reference", "")).strip().upper()
                    mycall = app.cfg.get("callsign", "").upper()
                    if not mycall:
                        self._send_json({"error": "No callsign configured"}, status=400)
                        return
                    if not reference:
                        self._send_json({"error": "Park reference required"}, status=400)
                        return
                    # Use payload values if provided, otherwise fall back to flrig
                    freq_khz = str(data.get("freq_khz", "")).strip() or None
                    if not freq_khz:
                        try:
                            freq_khz = str(round(float(app._flrig_freq_hz) / 1000)) if app._flrig_freq_hz else None
                        except Exception:
                            freq_khz = None
                    if not freq_khz:
                        self._send_json({"error": "Frequency unknown — connect Flrig or enter manually"}, status=400)
                        return
                    mode = str(data.get("mode", "")).strip().upper() or (
                        str(app._flrig_mode).upper() if app._flrig_mode else "SSB"
                    )
                    comment = str(data.get("comment", "")).strip() or "Self-spotted via POTA Hunter"
                    ok, err = pota_post_spot(
                        activator=mycall,
                        spotter=mycall,
                        reference=reference,
                        freq_khz=freq_khz,
                        mode=mode,
                        comment=comment,
                    )
                    if ok:
                        app.after(0, lambda: app._set_status("Self-spot posted ✔"))
                        self._send_json({"ok": True})
                    else:
                        self._send_json({"error": err or "POTA API error"}, status=502)
                elif self.path == '/log':
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                        data = json.loads(self.rfile.read(length))
                    except Exception:
                        self._send_json({"error": "bad json"}, status=400)
                        return
                    import queue as _queue
                    result_q = _queue.Queue()
                    app.after(0, lambda d=data, q=result_q: q.put(app._log_qso_from_web(d)))
                    try:
                        result = result_q.get(timeout=10)
                    except _queue.Empty:
                        self._send_json({"error": "timeout"}, status=500)
                        return
                    if result is True:
                        self._send_json({"ok": True})
                    else:
                        self._send_json({"error": result}, status=400)
                elif self.path == '/scan':
                    app.after(0, app._toggle_pota_scan)
                    self._send_json({"ok": True})
                elif self.path == '/scan_pause':
                    try:
                        length = int(self.headers.get('Content-Length', 0))
                        data = json.loads(self.rfile.read(length)) if length else {}
                    except Exception:
                        data = {}
                    app._scan_paused_by_map = bool(data.get('state', 0))
                    self._send_json({"ok": True})
                else:
                    self._send_json({"error": "not found"}, status=404)

            def log_message(self, fmt, *args):
                pass  # suppress access log noise

        class _ReuseServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True

        for port in (8765, 8766, 8767):
            try:
                server = _ReuseServer(("localhost", port), _Handler)
                self._map_server = server
                self._map_server_port = port
                t = threading.Thread(target=server.serve_forever, daemon=True)
                t.start()
                break
            except OSError:
                continue

    def _start_map_poll(self):
        self._stop_map_poll()
        self._do_map_poll()

    def _do_map_poll(self):
        try:
            mtime = os.path.getmtime(self.adif_path) if self.adif_path and os.path.exists(self.adif_path) else None
        except OSError:
            mtime = None
        if mtime != self._map_adif_mtime:
            self._map_adif_mtime = mtime
        if not self._pota_scan_active:
            self._refresh_map()
        self._map_poll_id = self.after(5000, self._do_map_poll)

    def _stop_map_poll(self):
        if self._map_poll_id:
            self.after_cancel(self._map_poll_id)
            self._map_poll_id = None

    # ── Tab 3: POTA Spots ─────────────────────────────────────────────────
    def _build_tab_pota(self, parent):
        PBGK = MAP_BG

        # Toolbar
        tb = tk.Frame(parent, bg=PBGK)
        tb.pack(fill="x", padx=6, pady=(4,2))
        tk.Label(tb, text="POTA ACTIVATORS", bg=PBGK, fg=ACCENT, font=LBL).pack(side="left")
        self._pota_status_lbl = tk.Label(tb, text="Not loaded", bg=PBGK, fg=FG2, font=SM,
                                         width=38, anchor="w")
        self._pota_status_lbl.pack(side="left", padx=12)
        tk.Label(tb, text="Band:", bg=PBGK, fg=FG2, font=SM).pack(side="left", padx=(0, 2))
        self._pota_band_cb = ttk.Combobox(
            tb, textvariable=self._pota_band_var,
            values=["All"], width=6, state="readonly", font=SM)
        self._pota_band_cb.pack(side="left")
        self._pota_band_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_pota_filters())
        tk.Label(tb, text="Mode:", bg=PBGK, fg=FG2, font=SM).pack(side="left", padx=(8, 2))
        self._pota_mode_cb = ttk.Combobox(
            tb, textvariable=self._pota_mode_var,
            values=["All"], width=7, state="readonly", font=SM)
        self._pota_mode_cb.pack(side="left")
        self._pota_mode_cb.bind("<<ComboboxSelected>>", lambda _: self._apply_pota_filters())
        ttk.Checkbutton(
            tb, text="Hide QRT", variable=self._pota_hide_qrt,
            command=self._apply_pota_filters).pack(side="left", padx=(10, 0))
        ttk.Checkbutton(
            tb, text="Hide OOB", variable=self._pota_hide_oob,
            command=self._apply_pota_filters).pack(side="left", padx=(6, 0))
        tk.Label(tb, text="ITU:", bg=PBGK, fg=FG2, font=SM).pack(side="left", padx=(10, 2))
        for _rgn, _var in (("R1", self._pota_itu_r1),
                            ("R2", self._pota_itu_r2),
                            ("R3", self._pota_itu_r3)):
            ttk.Checkbutton(tb, text=_rgn, variable=_var,
                            command=self._apply_pota_filters).pack(side="left", padx=(0, 2))
        self._pota_pause_btn = tk.Button(
            tb, text="⏸ Pause Updates", bg=BG3, fg=FG, font=SM,
            relief="flat", cursor="hand2", padx=8, width=16,
            command=self._toggle_pota_pause)
        self._pota_pause_btn.pack(side="right")
        tk.Button(tb, text="⟳ Refresh", bg=BG3, fg=FG, font=SM,
                  relief="flat", cursor="hand2", padx=8,
                  command=self._manual_pota_refresh).pack(side="right", padx=6)

        # Second toolbar row: scan controls and re-spot checkbox
        tb2 = tk.Frame(parent, bg=PBGK)
        tb2.pack(fill="x", padx=6, pady=(0, 2))
        ttk.Checkbutton(
            tb2, text="Auto re-spot QSO", variable=self._pota_respot_enabled
        ).pack(side="left", padx=(0, 12))
        ttk.Checkbutton(
            tb2, text="Skip worked", variable=self._pota_scan_skip_worked
        ).pack(side="left", padx=(0, 8))
        self._pota_scan_btn = tk.Button(
            tb2, text="▶ Scan", bg=BG3, fg=FG, font=SM,
            relief="flat", cursor="hand2", padx=8, width=12,
            command=self._toggle_pota_scan)
        self._pota_scan_btn.pack(side="left", padx=(0, 6))
        tk.Label(tb2, text="Interval:", bg=PBGK, fg=FG2, font=SM).pack(side="left", padx=(0, 2))
        tk.Spinbox(tb2, from_=5, to=60, increment=5,
                   textvariable=self._pota_scan_interval,
                   width=4, bg=BG3, fg=FG, font=SM,
                   relief="flat", justify="center",
                   buttonbackground=BG3).pack(side="left")
        tk.Label(tb2, text="s", bg=PBGK, fg=FG2, font=SM).pack(side="left")

        # Treeview
        frm = tk.Frame(parent, bg=PBGK)
        frm.pack(fill="both", expand=True, padx=6, pady=(2,6))

        pcols = ("Activator","Park","Park Name","Freq","Mode","Spotted","Comments")
        self._pota_tree = ttk.Treeview(frm, columns=pcols, show="headings",
                                       height=14, selectmode="browse")
        pwidths = [90, 80, 200, 85, 65, 75, 250]
        for col, w in zip(pcols, pwidths):
            self._pota_tree.heading(col, text=col)
            self._pota_tree.column(col, width=w, anchor="center", minwidth=40)
        self._pota_tree.column("Park Name", anchor="w")
        self._pota_tree.column("Comments",  anchor="w")

        pvsb = tk.Scrollbar(frm, orient="vertical",   command=self._pota_tree.yview, bg=BG3)
        phsb = tk.Scrollbar(frm, orient="horizontal", command=self._pota_tree.xview, bg=BG3)
        self._pota_tree.configure(yscrollcommand=pvsb.set, xscrollcommand=phsb.set)
        self._pota_tree.grid(row=0, column=0, sticky="nsew")
        pvsb.grid(row=0, column=1, sticky="ns")
        phsb.grid(row=1, column=0, sticky="ew")
        frm.rowconfigure(0, weight=1)
        frm.columnconfigure(0, weight=1)

        self._pota_tree.tag_configure("odd",    background=BG2, foreground=FG)
        self._pota_tree.tag_configure("even",   background=BG3, foreground=FG)
        self._pota_tree.tag_configure("tuned",  background=POTA_TUNED,  foreground="#000000")
        self._pota_tree.tag_configure("worked", background=POTA_WORKED, foreground="#000000", font=("Courier New", 10, "bold"))
        self._pota_tree.bind("<<TreeviewSelect>>", self._on_pota_spot_select)
        self._pota_tree.bind("<Button-1>", self._on_pota_tree_click)

    def _on_tab_changed(self, _=None):
        try:
            tab = self._nb.tab(self._nb.select(), "text").strip()
        except Exception:
            return
        if tab == "Grid Map":
            self._start_map_poll()
        else:
            self._stop_map_poll()
        if tab == "POTA Spots" and not self._pota_loaded:
            self._pota_loaded = True
            threading.Thread(target=self._fetch_pota_spots, daemon=True).start()

    def _fetch_pota_spots(self):
        try:
            req = urllib.request.Request(
                "https://api.pota.app/spot/activator",
                headers={"User-Agent": "POTA-Hunter/2.0"})
            with urllib.request.urlopen(req, timeout=12) as r:
                spots = json.loads(r.read().decode("utf-8"))
            spots.sort(
                key=lambda s: str(s.get("spotTime", s.get("timestamp", ""))),
                reverse=True)
            self._pota_spots_raw = spots
            self.after(0, self._apply_pota_filters)
        except Exception as e:
            self.after(0, self._pota_status_lbl.config,
                       {"text": f"⚠ Fetch failed: {e}", "fg": WARN})
        finally:
            if not self._pota_paused:
                self._pota_after_id = self.after(60_000, self._auto_refresh_pota)

    def _apply_pota_filters(self, *_):
        spots = self._pota_spots_raw

        band_sel = self._pota_band_var.get()
        if band_sel and band_sel != "All":
            def _mhz(s):
                try:
                    return float(s.get("frequency", s.get("freq", 0)) or 0) / 1000
                except Exception:
                    return 0.0
            spots = [s for s in spots if freq_to_band(_mhz(s)) == band_sel]

        mode_sel = self._pota_mode_var.get()
        if mode_sel and mode_sel != "All":
            spots = [s for s in spots
                     if str(s.get("mode", "")).upper() == mode_sel.upper()]

        if self._pota_hide_qrt.get():
            spots = [s for s in spots
                     if "qrt" not in str(
                         s.get("comments", s.get("comment", ""))).lower()]

        if self._pota_hide_oob.get():
            lic = self.cfg.get("license_class", "Extra")
            def _oob(s):
                try:
                    freq_mhz = float(s.get("frequency", s.get("freq", 0)) or 0) / 1000
                except Exception:
                    return False
                mode = str(s.get("mode", ""))
                return not _in_band(freq_mhz, lic, mode)
            spots = [s for s in spots if not _oob(s)]

        sel_regions = set()
        if self._pota_itu_r1.get(): sel_regions.add(1)
        if self._pota_itu_r2.get(): sel_regions.add(2)
        if self._pota_itu_r3.get(): sel_regions.add(3)
        if sel_regions and sel_regions != {1, 2, 3}:
            def _region(s):
                try:
                    return lat_lon_to_itu_region(float(s["latitude"]), float(s["longitude"]))
                except (KeyError, TypeError, ValueError):
                    return None
            spots = [s for s in spots if _region(s) in sel_regions]

        all_bands = sorted({
            freq_to_band(float(s.get("frequency", s.get("freq", 0)) or 0) / 1000)
            for s in self._pota_spots_raw
        } - {""})
        self._pota_band_cb["values"] = ["All"] + all_bands

        all_modes = sorted({
            str(s.get("mode", "")).upper()
            for s in self._pota_spots_raw
            if s.get("mode", "")
        })
        self._pota_mode_cb["values"] = ["All"] + all_modes

        self._populate_pota_table(spots)

    def _on_pota_tree_click(self, event=None):
        if self._pota_scan_active:
            self._stop_pota_scan()

    def _on_pota_spot_select(self, event=None):
        if getattr(self, '_suppress_pota_tune', False):
            self._suppress_pota_tune = False
            return
        if self._scan_paused_by_map:
            return
        sel = self._pota_tree.selection()
        if not sel:
            return
        # columns: Activator | Park | Park Name | Freq | Mode | Spotted | Comments
        values = self._pota_tree.item(sel[0], "values")
        activator = values[0]
        park      = values[1]
        park_name = values[2]  # Park name from POTA API
        freq_str  = values[3]
        self._pota_spot_ctx = {
            "activator": activator,
            "reference": park,
            "freq_khz":  freq_str,
            "mode":      values[4],
        }

        # Populate QSO entry fields
        self.e_call.delete(0, "end")
        self.e_call.insert(0, activator.upper())
        self.e_name.delete(0, "end")
        self.e_qth.delete(0, "end")
        self._qrz_info_lbl.config(text="")
        self.e_park.delete(0, "end")
        self.e_park.insert(0, park)

        # Look up park in database and populate grid square
        park_data = lookup_park(park)
        if park_data and park_data.get("grid"):
            self.e_grid.delete(0, "end")
            self.e_grid.insert(0, park_data["grid"][:4])  # Maidenhead grid square (4 characters)
        
        # Update park info label with park name from spot data
        if park_name:
            self._park_info_lbl.config(text=f"{park_name} ({park})", fg=ACC3)
        else:
            self._park_info_lbl.config(text=f"Park {park}", fg=MUTED)

        # Tune radio — POTA API returns frequency in kHz (e.g. 14225 = 14.225 MHz)
        try:
            freq_khz = float(freq_str)
            freq_hz = int(freq_khz * 1_000)
            freq_mhz_disp = f"{freq_khz / 1000:.4f}"
        except (ValueError, TypeError):
            return

        # Out-of-band check for user's license class
        spot_mode = values[4] if len(values) > 4 else ""
        freq_mhz = freq_khz / 1000.0
        lic = self.cfg.get("license_class", "Extra")
        if not _in_band(freq_mhz, lic, spot_mode):
            self.after(0, lambda f=freq_mhz: self._warn_out_of_band(f))

        # Immediately highlight this row and suppress the flrig poll
        # from overwriting it before the tune command completes.
        self._last_tuned_spot_key = (activator.strip().upper(), park.strip())
        self._pota_clicked_hz = freq_hz
        self._tune_suppress_until = time.monotonic() + 4.0
        self._refresh_pota_highlights()
        self._pota_tree.selection_set([])
        host = self.cfg["flrig_host"]
        port = self.cfg["flrig_port"]
        self._pota_status_lbl.config(text=f"Tuning to {freq_mhz_disp} MHz…", fg=FG2)

        def _tune():
            result = flrig_set_freq_and_mode(host, port, freq_hz, spot_mode)
            if result is True:
                msg = f"Tuned → {freq_mhz_disp} MHz"
                fg  = ACC3
                self._tune_suppress_until = time.monotonic() + 3.0
                self.after(0, lambda: self._update_vfo_display(freq_hz, self._flrig_mode, force=True))
            else:
                msg = f"Tune failed: {result}"
                fg  = WARN
            self.after(0, lambda: self._pota_status_lbl.config(text=msg, fg=fg))

        threading.Thread(target=_tune, daemon=True).start()

    def _on_map_station_click(self, data):
        if self._pota_scan_active:
            self._stop_pota_scan()
        if self._scan_paused_by_map:
            return
        if data.get("tuned"):
            return
        try:
            freq_khz = float(data.get("freq_khz", 0))
            freq_hz  = int(freq_khz * 1_000)
        except (ValueError, TypeError):
            return
        activator = str(data.get("activator", "")).strip().upper()
        park      = str(data.get("park", "")).strip()
        for iid in self._pota_tree.get_children():
            vals = self._pota_tree.item(iid, "values")
            if (str(vals[0]).strip().upper() == activator and
                    str(vals[1]).strip() == park):
                self._pota_tree.selection_set(iid)
                self._pota_tree.see(iid)
                self._on_pota_spot_select()
                return
        freq_mhz_disp = f"{freq_khz / 1000:.4f}"
        self._pota_clicked_hz     = freq_hz
        self._tune_suppress_until = time.monotonic() + 4.0
        self._refresh_pota_highlights()
        host = self.cfg["flrig_host"]
        port = self.cfg["flrig_port"]
        self._pota_status_lbl.config(text=f"Tuning to {freq_mhz_disp} MHz…", fg=FG2)

        map_mode = str(data.get("mode", "")).strip()
        def _tune():
            result = flrig_set_freq_and_mode(host, port, freq_hz, map_mode)
            if result is True:
                self._tune_suppress_until = time.monotonic() + 3.0
                self.after(0, lambda: self._update_vfo_display(freq_hz, self._flrig_mode, force=True))
                self.after(0, lambda: self._pota_status_lbl.config(
                    text=f"Tuned → {freq_mhz_disp} MHz", fg=ACC3))
            else:
                self.after(0, lambda: self._pota_status_lbl.config(
                    text=f"Tune failed: {result}", fg=WARN))

        threading.Thread(target=_tune, daemon=True).start()

    def _populate_pota_table(self, spots):
        self._pota_spots_filtered = list(spots)
        # The selection is cleared after every tune, so track the last-tuned
        # spot key instead of relying on the live tree selection.
        restore_key = self._last_tuned_spot_key
        self._pota_tree.delete(*self._pota_tree.get_children())
        for s in spots:
            act   = s.get("activator",  s.get("activatorCallsign", ""))
            park  = s.get("reference",  s.get("parkReference", ""))
            pname = s.get("name",       s.get("parkName", ""))
            freq  = s.get("frequency",  s.get("freq", ""))
            mode  = s.get("mode", "")
            stime = s.get("spotTime",   s.get("timestamp", ""))
            if stime and "T" in str(stime):
                stime = str(stime).split("T")[1][:5] + "z"
            cmts  = s.get("comments",   s.get("comment", ""))
            self._pota_tree.insert("", "end",
                values=(act, park, pname, freq, mode, stime, cmts))
        # Restore the last-tuned row so arrow-key navigation resumes from the
        # correct station after a refresh.  If the station is gone from the
        # refreshed list, leave nothing selected rather than jumping elsewhere.
        if restore_key:
            for iid in self._pota_tree.get_children():
                v = self._pota_tree.item(iid, "values")
                if (str(v[0]).strip().upper(), str(v[1]).strip()) == restore_key:
                    self._suppress_pota_tune = True
                    self._pota_tree.selection_set(iid)
                    self._pota_tree.focus(iid)
                    break
        self._refresh_pota_highlights()
        self._check_freq_conflict()
        now = datetime.datetime.now(datetime.timezone.utc).strftime("%H:%M:%Sz")
        self._pota_status_lbl.config(
            text=f"● {len(spots)} activators  last updated {now}", fg=ACC3)

    def _refresh_pota_highlights(self):
        try:
            rows = self.conn.execute(
                "SELECT DISTINCT UPPER(TRIM(call)) FROM qso").fetchall()
            worked = {r[0] for r in rows if r[0]}
        except Exception:
            worked = set()
        # During the suppress window (user just clicked a spot) always use the
        # clicked frequency so the flrig poll can't overwrite the highlight
        # before the tune command finishes.  Outside that window, prefer the
        # live VFO; fall back to the last-clicked spot when flrig is offline.
        if time.monotonic() < self._tune_suppress_until and self._pota_clicked_hz:
            vfo_hz = self._pota_clicked_hz
        else:
            vfo_hz = self._flrig_freq_hz if self._flrig_freq_hz is not None else self._pota_clicked_hz
        for i, iid in enumerate(self._pota_tree.get_children()):
            vals = self._pota_tree.item(iid, "values")
            activator = str(vals[0]).strip().upper()
            base = "even" if i % 2 == 0 else "odd"
            if activator in worked:
                self._pota_tree.item(iid, tags=("worked",))
                continue
            if vfo_hz is not None:
                try:
                    spot_hz = int(float(vals[3]) * 1000)  # kHz → Hz, exact
                    if spot_hz == int(vfo_hz):
                        self._pota_tree.item(iid, tags=("tuned",))
                        continue
                except (ValueError, TypeError):
                    pass
            self._pota_tree.item(iid, tags=(base,))
        self._refresh_map()

    def _auto_refresh_pota(self):
        if self._pota_paused:
            return
        self._pota_status_lbl.config(text="Refreshing…", fg=FG2)
        threading.Thread(target=self._fetch_pota_spots, daemon=True).start()

    def _manual_pota_refresh(self):
        self._pota_status_lbl.config(text="Refreshing…", fg=FG2)
        if self._pota_after_id:
            self.after_cancel(self._pota_after_id)
            self._pota_after_id = None
        self._pota_loaded = True
        threading.Thread(target=self._fetch_pota_spots, daemon=True).start()

    def _toggle_pota_pause(self):
        self._pota_paused = not self._pota_paused
        if self._pota_paused:
            self._pota_pause_btn.config(text="▶ Resume Updates", fg=ACCENT)
            if self._pota_after_id:
                self.after_cancel(self._pota_after_id)
                self._pota_after_id = None
        else:
            self._pota_pause_btn.config(text="⏸ Pause Updates", fg=FG)
            self._pota_after_id = self.after(60_000, self._auto_refresh_pota)

    def _toggle_pota_scan(self):
        if self._pota_scan_active:
            self._stop_pota_scan()
        else:
            self._start_pota_scan()

    def _start_pota_scan(self):
        if not self._pota_tree.get_children():
            return
        self._pota_scan_active = True
        self._pota_scan_idx    = 0
        self._pota_scan_btn.config(text="⏹ Stop Scan", fg=ACCENT)
        self._start_map_scan_blink()
        self._pota_scan_step()

    def _stop_pota_scan(self):
        self._pota_scan_active = False
        self._pota_scan_btn.config(text="▶ Scan", fg=FG)
        if self._pota_scan_after_id:
            self.after_cancel(self._pota_scan_after_id)
            self._pota_scan_after_id = None
        if self._map_scan_blink_id:
            self.after_cancel(self._map_scan_blink_id)
            self._map_scan_blink_id = None
        if hasattr(self, '_map_canvas'):
            self._map_canvas.delete("scan_label")

    def _pota_scan_step(self):
        if not self._pota_scan_active:
            return
        interval_ms = max(5, min(60, self._pota_scan_interval.get())) * 1_000
        if self._flrig_ptt or self._scan_paused_by_map:
            self._pota_scan_after_id = self.after(interval_ms, self._pota_scan_step)
            return
        children = self._pota_tree.get_children()
        if not children:
            self._stop_pota_scan()
            return
        if self._pota_scan_idx >= len(children):
            self._pota_scan_idx = 0
        if self._pota_scan_skip_worked.get():
            try:
                rows = self.conn.execute(
                    "SELECT DISTINCT UPPER(TRIM(call)) FROM qso").fetchall()
                worked = {r[0] for r in rows if r[0]}
            except Exception:
                worked = set()
            for _ in range(len(children)):
                iid = children[self._pota_scan_idx]
                activator = str(self._pota_tree.item(iid, "values")[0]).strip().upper()
                if activator not in worked:
                    break
                self._pota_scan_idx = (self._pota_scan_idx + 1) % len(children)
        iid = children[self._pota_scan_idx]
        self._pota_tree.selection_set(iid)
        self._pota_tree.see(iid)
        self._pota_scan_idx += 1
        self._refresh_map()
        self._pota_scan_after_id = self.after(interval_ms, self._pota_scan_step)

    # ── Entry form ────────────────────────────────────────────────────────
    def _build_entry_form(self, parent):
        ent = dict(bg=BG3, fg=FG, font=MONO, relief="flat",
                   insertbackground=ACCENT, bd=4)
        lbl_kw = dict(bg=BG, fg=FG2, font=SM, anchor="w")

        f = tk.Frame(parent, bg=BG)
        f.pack(fill="x", padx=10, pady=(8,4))

        labels      = ["Callsign *", "Name", "City / State", "RST Sent", "RST Rcvd",
                       "Park #", "Grid", "Comments", "Notes"]
        col_weights = [0, 0, 0, 0, 0, 0, 0, 1, 1]
        for i, (text, wt) in enumerate(zip(labels, col_weights)):
            tk.Label(f, text=text, **lbl_kw).grid(
                row=0, column=i, sticky="w",
                padx=(0 if i==0 else 10, 0))
            if wt:
                f.columnconfigure(i, weight=wt)

        self.e_call = tk.Entry(f, width=11, **ent)
        self.e_call.bind("<FocusOut>", self._on_call_focusout)
        self.e_call.bind("<Return>",   self._log_qso)
        self.e_call.grid(row=1, column=0, padx=(0,4), sticky="w")

        self.e_name = tk.Entry(f, width=18, **ent)
        self.e_name.bind("<Return>", self._log_qso)
        self.e_name.grid(row=1, column=1, padx=(10,4), sticky="w")

        self.e_qth = tk.Entry(f, width=15, **ent)
        self.e_qth.bind("<Return>", self._log_qso)
        self.e_qth.grid(row=1, column=2, padx=(10,4), sticky="w")

        self.e_rst_s = tk.Entry(f, width=5, **ent)
        self.e_rst_s.insert(0,"59")
        self.e_rst_s.bind("<Return>", self._log_qso)
        self.e_rst_s.grid(row=1, column=3, padx=(10,4), sticky="w")

        self.e_rst_r = tk.Entry(f, width=5, **ent)
        self.e_rst_r.insert(0,"59")
        self.e_rst_r.bind("<Return>", self._log_qso)
        self.e_rst_r.grid(row=1, column=4, padx=(10,4), sticky="w")

        self.e_park = tk.Entry(f, width=11, **ent)
        self.e_park.bind("<Return>",   self._log_qso)
        self.e_park.bind("<FocusOut>", self._on_park_focusout)
        self.e_park.grid(row=1, column=5, padx=(10,4), sticky="w")

        self.e_grid = tk.Entry(f, width=7, **ent)
        self.e_grid.bind("<Return>", self._log_qso)
        self.e_grid.grid(row=1, column=6, padx=(10,4), sticky="w")

        self.e_comment = tk.Entry(f, width=22, **ent)
        self.e_comment.bind("<Return>", self._log_qso)
        self.e_comment.grid(row=1, column=7, padx=(10,4), sticky="ew")

        self.e_notes = tk.Entry(f, width=22, **ent)
        self.e_notes.bind("<Return>", self._log_qso)
        self.e_notes.grid(row=1, column=8, padx=(10,4), sticky="ew")

        info_row = tk.Frame(parent, bg=BG)
        info_row.pack(fill="x", padx=10, pady=(2,0))
        self._qrz_info_lbl = tk.Label(info_row, text="", bg=BG, fg=ACC3, font=SM,
                                       width=40, anchor="w")
        self._qrz_info_lbl.pack(side="left")
        self._park_info_lbl = tk.Label(info_row, text="", bg=BG, fg=MUTED, font=SM,
                                       width=50, anchor="w")
        self._park_info_lbl.pack(side="left", padx=(12, 0))
        self._rig_snap_lbl = tk.Label(info_row,
            text="Freq / Band / Mode will be captured from Flrig when LOG QSO is pressed.",
            bg=BG, fg=MUTED, font=SM)
        self._rig_snap_lbl.pack(side="right")

        btn_row = tk.Frame(parent, bg=BG)
        btn_row.pack(fill="x", padx=10, pady=(4,8))
        bc = dict(font=LBL, relief="flat", cursor="hand2", pady=5, padx=16)
        self._reticle_img = _make_reticle_img(20, BG, ACCENT)
        tk.Button(btn_row, text=" Snipe QSO", image=self._reticle_img,
                  compound="left", bg=ACCENT, fg=BG,
                  command=self._log_qso, **bc).pack(side="left")
        tk.Button(btn_row, text="⊕ Multi-Op", bg=BG2, fg=FG,
                  command=self._multi_op_qso, **bc).pack(side="left", padx=8)
        tk.Button(btn_row, text="✕ Clear Form", bg=BG3, fg=FG2,
                  command=self._clear_form, **bc).pack(side="left")

        tk.Label(btn_row, text="Check for clear freq kHz:", bg=BG, fg=FG2, font=LBL).pack(side="left", padx=(12, 2))
        self._freq_check_border = tk.Frame(btn_row, bg=MUTED)
        self._freq_check_border.pack(side="left")
        freq_entry = tk.Entry(self._freq_check_border, textvariable=self._freq_check_var, width=7,
                              bg=BG2, fg=FG, insertbackground=FG, font=LBL,
                              bd=0, highlightthickness=0)
        freq_entry.pack(padx=3, pady=3)
        freq_entry.bind("<Return>", lambda _: self._check_freq_conflict())
        freq_entry.bind("<KeyRelease>", lambda e: self._reset_freq_border() if e.keysym != "Return" else None)
        self._freq_check_label = tk.Label(btn_row, text="", bg=BG, fg=FG2, font=LBL, width=5, anchor="w")
        self._freq_check_label.pack(side="left", padx=(4, 0))

        self.e_call.focus_set()

    # ── Form helpers ──────────────────────────────────────────────────────
    def _on_call_focusout(self, _=None):
        call = self.e_call.get().strip().upper()
        if not call:
            return
        self.e_call.delete(0,"end")
        self.e_call.insert(0, call)
        self._qrz_info_lbl.config(text="")
        threading.Thread(target=self._qrz_lookup_bg,
                         args=(call,), daemon=True).start()

    def _qrz_lookup_bg(self, call):
        # Tier 1: local FCC database (instant, offline, US callsigns)
        info = fcc_lookup(call)
        # Tier 2: HamDB free API (non-US callsigns, or FCC DB not built yet)
        if not info:
            info = hamdb_lookup(call)
        # Tier 3: QRZ (requires subscription, broadest coverage)
        if not info:
            qrz = qrz_lookup(call)
            if qrz:
                qrz["source"] = "QRZ"
                info = qrz
        # If FCC found it but has no grid, try HamDB for the grid square
        if info and info.get("source") == "FCC" and not info.get("grid"):
            hdb = hamdb_lookup(call)
            if hdb and hdb.get("grid"):
                info["grid"] = hdb["grid"]
        if info:
            self.after(0, lambda: self._apply_qrz_info(info))

    def _apply_qrz_info(self, info):
        source = info.get("source", "")
        name   = info.get("name", "")
        qth    = info.get("qth", "")
        grid   = info.get("grid", "")
        cls    = info.get("class", "")
        CLASS_FULL = {"E": "Extra", "A": "Advanced", "G": "General",
                      "T": "Technician", "N": "Novice", "P": "Tech+"}
        cls_str = CLASS_FULL.get(cls, cls)
        parts = []
        if name:
            parts.append(f"{name} ({cls_str})" if cls_str else name)
        if qth:
            parts.append(qth)
        if grid:
            parts.append(grid)
        prefix = f"[{source}] " if source else ""
        self._qrz_info_lbl.config(
            text=(prefix + "  ".join(parts)) if parts else "")
        if name and not self.e_name.get().strip():
            self.e_name.delete(0, "end")
            self.e_name.insert(0, name)
        if qth and not self.e_qth.get().strip():
            self.e_qth.delete(0, "end")
            self.e_qth.insert(0, qth)
        if grid and not self.e_grid.get().strip():
            self.e_grid.delete(0, "end")
            self.e_grid.insert(0, grid[:4])

    def _on_park_focusout(self, _=None):
        ref = self.e_park.get().strip().upper()
        if not ref:
            self._park_info_lbl.config(text="", fg=MUTED)
            return
        self.e_park.delete(0, "end")
        self.e_park.insert(0, ref)
        if not parks_db_exists():
            self._park_info_lbl.config(
                text="Parks DB not built — use Settings > Update POTA Parks DB",
                fg=WARN)
            return
        self._park_info_lbl.config(text="Looking up…", fg=MUTED)
        threading.Thread(target=self._park_lookup_bg,
                         args=(ref,), daemon=True).start()

    def _park_lookup_bg(self, ref):
        info = lookup_park(ref)
        self.after(0, lambda: self._apply_park_info(ref, info))

    def _apply_park_info(self, ref, info):
        if info is None:
            self._park_info_lbl.config(text=f"Park {ref} not found in DB", fg=WARN)
            return
        grid  = info.get("grid", "")
        name  = info.get("name", "")
        state = info.get("state", "")
        if grid and not self.e_grid.get().strip():
            self.e_grid.delete(0, "end")
            self.e_grid.insert(0, grid[:4])
        parts = [p for p in (name, state) if p]
        label = " — ".join(parts) + (f"  [{grid}]" if grid else "") if parts else ref
        self._park_info_lbl.config(text=label, fg=ACC3)

    def _clear_form(self):
        for w in (self.e_call, self.e_name, self.e_qth, self.e_park, self.e_grid,
                  self.e_comment, self.e_notes):
            w.delete(0,"end")
        self.e_rst_s.delete(0,"end"); self.e_rst_s.insert(0,"59")
        self.e_rst_r.delete(0,"end"); self.e_rst_r.insert(0,"59")
        self._qrz_info_lbl.config(text="")
        self._park_info_lbl.config(text="", fg=MUTED)
        self._pota_spot_ctx = None
        self.e_call.focus_set()

    def _reset_freq_border(self):
        if self._freq_check_border is not None:
            self._freq_check_border.config(bg=MUTED)
        if self._freq_check_label is not None:
            self._freq_check_label.config(text="", fg=FG2)

    def _check_freq_conflict(self):
        if self._freq_check_border is None:
            return
        raw = self._freq_check_var.get().strip()
        if not raw:
            self._freq_check_border.config(bg=MUTED)
            return
        try:
            entered_khz = float(raw)
        except ValueError:
            self._freq_check_border.config(bg=MUTED)
            return
        valid = [float(s.get("frequency", s.get("freq", 0)))
                 for s in self._pota_spots_raw
                 if s.get("frequency", s.get("freq")) not in (None, "", 0)]
        if not valid:
            self._freq_check_border.config(bg=MUTED)
            if self._freq_check_label is not None:
                self._freq_check_label.config(text="", fg=FG2)
            return
        min_dist = min(abs(entered_khz - f) for f in valid)
        if min_dist < 2:
            color, label_text, label_fg = WARN,   "No",    WARN
        elif min_dist < 3:
            color, label_text, label_fg = YELLOW, "Maybe", YELLOW
        else:
            color, label_text, label_fg = ACC3,   "Yes",   ACC3
        self._freq_check_border.config(bg=color)
        if self._freq_check_label is not None:
            self._freq_check_label.config(text=label_text, fg=label_fg)

    # ── Log QSO ───────────────────────────────────────────────────────────
    def _log_qso(self, _=None):
        if not self.adif_path:
            messagebox.showwarning("No Logbook", "Open or create a logbook first.")
            return
        call = self.e_call.get().strip().upper()
        if not call:
            messagebox.showerror("Required", "Callsign is required.")
            return

        now      = datetime.datetime.now(datetime.timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M")

        if self._flrig_freq_hz is not None:
            try:
                freq_mhz = float(self._flrig_freq_hz) / 1_000_000
            except Exception:
                freq_mhz = float(self._flrig_freq_hz)
            band = freq_to_band(freq_mhz)
            mode = str(self._flrig_mode).upper() if self._flrig_mode else ""
        else:
            freq_mhz = None
            band     = ""
            mode     = ""

        row = {
            "call":       call,
            "date":       date_str,
            "time_on":    time_str,
            "freq":       freq_mhz,
            "band":       band,
            "mode":       mode,
            "rst_sent":   self.e_rst_s.get().strip() or "59",
            "rst_rcvd":   self.e_rst_r.get().strip() or "59",
            "name":       self.e_name.get().strip(),
            "qth":        self.e_qth.get().strip(),
            "gridsquare": self.e_grid.get().strip().upper(),
            "park_nr":    self.e_park.get().strip(),
            "comment":    self.e_comment.get().strip(),
            "notes":      self.e_notes.get().strip(),
        }

        self.conn.execute("""
            INSERT INTO qso (call,date,time_on,freq,band,mode,
                rst_sent,rst_rcvd,name,qth,gridsquare,park_nr,comment,notes)
            VALUES (:call,:date,:time_on,:freq,:band,:mode,
                :rst_sent,:rst_rcvd,:name,:qth,:gridsquare,:park_nr,:comment,:notes)
        """, row)
        self.conn.commit()

        mycall = self.cfg.get("callsign","").upper()
        with open(self.adif_path, "a", encoding="utf-8") as f:
            f.write(row_to_adif(row, mycall))

        freq_disp = f"{freq_mhz:.4f} MHz" if freq_mhz else "freq unknown"
        snap = (f"Last: {call}  {date_str} {time_str}z  "
                f"{freq_disp}  {band}  {mode}")
        self._rig_snap_lbl.config(text=snap, fg=ACC3)
        self._set_status(f"Logged ✔  {call}  {date_str} {time_str}z  {freq_disp}  {band}  {mode}")
        self._reload_table()
        self._refresh_pota_highlights()
        self._maybe_post_pota_spot(row)
        self._clear_form()

    def _multi_op_qso(self):
        """Log QSO then restore park/grid so the next contact at the same park is ready."""
        park = self.e_park.get().strip()
        grid = self.e_grid.get().strip()
        self._log_qso()
        self.e_park.delete(0, "end"); self.e_park.insert(0, park)
        self.e_grid.delete(0, "end"); self.e_grid.insert(0, grid)

    def _log_qso_from_web(self, data):
        """Log a QSO submitted from the HTML map page. Returns True or an error string."""
        if not self.adif_path:
            return "No logbook open"
        call = str(data.get("call", "")).strip().upper()
        if not call:
            return "Callsign required"

        now      = datetime.datetime.now(datetime.timezone.utc)
        date_str = now.strftime("%Y-%m-%d")
        time_str = now.strftime("%H%M")

        try:
            freq_khz = float(data.get("freq_khz") or 0)
            freq_mhz = freq_khz / 1000.0 if freq_khz else None
        except (TypeError, ValueError):
            freq_mhz = None

        band = freq_to_band(freq_mhz) if freq_mhz else ""
        mode = str(data.get("mode", "")).strip().upper()

        row = {
            "call":       call,
            "date":       date_str,
            "time_on":    time_str,
            "freq":       freq_mhz,
            "band":       band,
            "mode":       mode,
            "rst_sent":   str(data.get("rst_sent", "59")).strip() or "59",
            "rst_rcvd":   str(data.get("rst_rcvd", "59")).strip() or "59",
            "name":       str(data.get("name", "")).strip(),
            "qth":        str(data.get("qth", "")).strip(),
            "gridsquare": str(data.get("gridsquare", "")).strip().upper(),
            "park_nr":    str(data.get("park", "")).strip(),
            "comment":    str(data.get("comment", "")).strip(),
            "notes":      "",
        }

        try:
            self.conn.execute("""
                INSERT INTO qso (call,date,time_on,freq,band,mode,
                    rst_sent,rst_rcvd,name,qth,gridsquare,park_nr,comment,notes)
                VALUES (:call,:date,:time_on,:freq,:band,:mode,
                    :rst_sent,:rst_rcvd,:name,:qth,:gridsquare,:park_nr,:comment,:notes)
            """, row)
            self.conn.commit()

            mycall = self.cfg.get("callsign", "").upper()
            with open(self.adif_path, "a", encoding="utf-8") as f:
                f.write(row_to_adif(row, mycall))

            self._reload_table()
            self._refresh_pota_highlights()
            self._maybe_post_pota_spot(row)
            freq_disp = f"{freq_mhz:.4f} MHz" if freq_mhz else "freq unknown"
            self._set_status(f"Web logged ✔  {call}  {date_str} {time_str}z  {freq_disp}  {band}  {mode}")
            return True
        except Exception as exc:
            return str(exc)

    def _maybe_post_pota_spot(self, row):
        if not self._pota_respot_enabled.get():
            return
        ctx = self._pota_spot_ctx
        if not ctx:
            return
        mycall = self.cfg.get("callsign", "").upper()
        if not mycall:
            return
        freq_khz = ctx.get("freq_khz") or (
            str(round(row["freq"] * 1000)) if row.get("freq") else ""
        )
        mode = ctx.get("mode") or row.get("mode", "")
        def bg():
            ok, err = pota_post_spot(
                activator=ctx["activator"],
                spotter=mycall,
                reference=ctx["reference"],
                freq_khz=freq_khz,
                mode=mode,
            )
            msg = "POTA spot posted ✔" if ok else f"POTA spot failed: {err}"
            fg  = ACC3 if ok else WARN
            self.after(0, lambda: self._set_status(msg, fg))
        threading.Thread(target=bg, daemon=True).start()

    # ── Table ─────────────────────────────────────────────────────────────
    def _reload_table(self, rows=None):
        self._tree.delete(*self._tree.get_children())
        if rows is None:
            rows = self.conn.execute(
                "SELECT * FROM qso ORDER BY date DESC, time_on DESC").fetchall()
        for i, row in enumerate(rows):
            tag = "even" if i % 2 == 0 else "odd"
            freq_str = f"{row['freq']:.4f}" if row["freq"] else ""
            self._tree.insert("","end", iid=str(row["id"]),
                values=(row["id"], row["call"], row["date"], row["time_on"],
                        freq_str, row["band"] or "", row["mode"] or "",
                        row["rst_sent"] or "", row["rst_rcvd"] or "",
                        row["park_nr"] or "", row["comment"] or "",
                        row["notes"] or ""),
                tags=(tag,))
        n = len(rows)
        self._qso_count_lbl.config(text=f"{n} QSO{'s' if n!=1 else ''}")
        # Always attempt a map refresh; _refresh_map guards internally
        self._refresh_map()

    def _apply_filter(self):
        q    = self._search_var.get().strip()
        band = self._filter_band.get()
        mode = self._filter_mode.get()
        sql  = "SELECT * FROM qso WHERE 1=1"
        params = []
        if q:
            like = f"%{q}%"
            sql += (" AND (call LIKE ? OR comment LIKE ? OR notes LIKE ?"
                    " OR park_nr LIKE ?)")
            params += [like, like, like, like]
        if band and band != "All":
            sql += " AND band=?"; params.append(band)
        if mode and mode != "All":
            sql += " AND mode=?"; params.append(mode)
        sql += " ORDER BY date DESC, time_on DESC"
        self._reload_table(self.conn.execute(sql, params).fetchall())

    def _clear_filter(self):
        self._search_var.set("")
        self._filter_band.set("All")
        self._filter_mode.set("All")
        self._reload_table()

    def _sort_by(self, col):
        col_map = {
            "id":"id","Call":"call","Date":"date","Time UTC":"time_on",
            "Freq MHz":"freq","Band":"band","Mode":"mode",
            "RST Snt":"rst_sent","RST Rcv":"rst_rcvd","Park #":"park_nr",
            "Comments":"comment","Notes":"notes",
        }
        rows = self.conn.execute(
            f"SELECT * FROM qso ORDER BY {col_map.get(col,'id')} ASC").fetchall()
        self._reload_table(rows)

    def _on_qso_select(self, _=None):
        sel = self._tree.selection()
        if sel:
            row = self.conn.execute("SELECT * FROM qso WHERE id=?",
                                    (int(sel[0]),)).fetchone()
            if row:
                self._set_status(
                    f"Selected: {row['call']}  {row['date']} {row['time_on']}z"
                    f"  {row['band']}  {row['mode']}")

    # ── Edit / Delete ─────────────────────────────────────────────────────
    def _edit_qso(self, _=None):
        sel = self._tree.selection()
        if not sel:
            return
        row = self.conn.execute("SELECT * FROM qso WHERE id=?",
                                (int(sel[0]),)).fetchone()
        if row:
            EditDialog(self, self.conn, self.adif_path,
                       self.cfg.get("callsign",""), row, self._reload_table)

    def _delete_qso(self):
        sel = self._tree.selection()
        if not sel:
            return
        qso_id = int(sel[0])
        row = self.conn.execute("SELECT call,date FROM qso WHERE id=?",
                                (qso_id,)).fetchone()
        if not messagebox.askyesno("Delete",
                f"Delete QSO with {row['call']} on {row['date']}?"):
            return
        self.conn.execute("DELETE FROM qso WHERE id=?", (qso_id,))
        self.conn.commit()
        rewrite_adif(self.adif_path, self.conn,
                     self.cfg.get("callsign","").upper())
        load_adif_into_index(self.adif_path, self.conn)
        self._reload_table()
        self._set_status(f"Deleted QSO #{qso_id}")

    # ── Logbook management ────────────────────────────────────────────────
    def _prompt_logbook(self):
        if messagebox.askyesno("POTA Hunter",
                "No logbook open.\nCreate a new logbook?"):
            self._new_logbook()
        else:
            self._choose_logbook()

    def _new_logbook(self):
        name = simpledialog.askstring("New Logbook",
            "Logbook name (e.g. General, POTA-2026):", parent=self)
        if not name:
            return
        safe = re.sub(r'[^\w\-]', '_', name.strip())
        path = os.path.join(LOGBOOK_DIR, safe + ".adi")
        if os.path.exists(path):
            if not messagebox.askyesno("Exists", f"{safe}.adi exists. Open it?"):
                return
        self._open_adif(path)

    def _choose_logbook(self):
        path = filedialog.askopenfilename(
            title="Open Logbook",
            initialdir=LOGBOOK_DIR,
            filetypes=[("ADIF Logbook","*.adi *.adif"),("All","*.*")])
        if path:
            self._open_adif(path)

    def _open_adif(self, path):
        self.adif_path = path
        if not os.path.exists(path):
            mycall = self.cfg.get("callsign","").upper()
            with open(path,"w",encoding="utf-8") as f:
                f.write(adif_header(mycall))
        count = load_adif_into_index(path, self.conn)
        name  = os.path.splitext(os.path.basename(path))[0]
        self._logbook_lbl.config(text=f"Logbook: {name}  [{path}]", fg=ACC3)
        self.title(f"POTA Hunter — {name}")
        self.cfg["last_logbook"] = path
        save_config(self.cfg)
        self._reload_table()
        self._set_status(f"Opened '{name}'  ({count} QSOs)")

    # ── ADIF export / import ──────────────────────────────────────────────
    def _export_adif(self):
        if not self.adif_path:
            messagebox.showwarning("No Logbook","Open a logbook first.")
            return
        dest = filedialog.asksaveasfilename(
            title="Export ADIF copy",
            defaultextension=".adi",
            filetypes=[("ADIF","*.adi *.adif"),("All","*.*")],
            initialdir=LOGBOOK_DIR)
        if not dest:
            return
        mycall = self.cfg.get("callsign","").upper()
        rewrite_adif(dest, self.conn, mycall)
        n = self.conn.execute("SELECT COUNT(*) FROM qso").fetchone()[0]
        self._set_status(f"Exported {n} QSOs → {os.path.basename(dest)}")
        messagebox.showinfo("Export Complete", f"Exported {n} QSOs to:\n{dest}")

    def _import_adif(self):
        if not self.adif_path:
            messagebox.showwarning("No Logbook","Open a logbook first.")
            return
        src = filedialog.askopenfilename(
            title="Import ADIF",
            filetypes=[("ADIF","*.adi *.adif"),("All","*.*")])
        if not src:
            return
        with open(src,"r",encoding="utf-8",errors="replace") as f:
            text = f.read()
        records = parse_adif_records(text)
        mycall  = self.cfg.get("callsign","").upper()
        for d in records:
            row = adif_to_row_dict(d)
            self.conn.execute("""
                INSERT INTO qso (call,date,time_on,freq,band,mode,
                    rst_sent,rst_rcvd,name,qth,gridsquare,park_nr,comment,notes)
                VALUES (:call,:date,:time_on,:freq,:band,:mode,
                    :rst_sent,:rst_rcvd,:name,:qth,:gridsquare,:park_nr,:comment,:notes)
            """, row)
        self.conn.commit()
        rewrite_adif(self.adif_path, self.conn, mycall)
        self._reload_table()
        self._set_status(f"Imported {len(records)} QSOs from {os.path.basename(src)}")
        messagebox.showinfo("Import Complete", f"Imported {len(records)} QSOs.")

    # ── Settings ──────────────────────────────────────────────────────────
    def _station_settings(self): StationDialog(self, self.cfg)
    def _qrz_settings(self):     QRZDialog(self, self.cfg)
    def _flrig_settings(self):   FlrigDialog(self, self.cfg)

    def _save_filter_settings(self):
        self.cfg["pota_band"]             = self._pota_band_var.get()
        self.cfg["pota_mode"]             = self._pota_mode_var.get()
        self.cfg["pota_hide_qrt"]         = self._pota_hide_qrt.get()
        self.cfg["pota_hide_oob"]         = self._pota_hide_oob.get()
        self.cfg["pota_itu_r1"]           = self._pota_itu_r1.get()
        self.cfg["pota_itu_r2"]           = self._pota_itu_r2.get()
        self.cfg["pota_itu_r3"]           = self._pota_itu_r3.get()
        self.cfg["pota_respot_enabled"]   = self._pota_respot_enabled.get()
        self.cfg["pota_scan_skip_worked"] = self._pota_scan_skip_worked.get()
        self.cfg["pota_scan_interval"]    = self._pota_scan_interval.get()
        save_config(self.cfg)
        self._set_status("Filter settings saved.")

    def _update_parks_db(self):
        if not messagebox.askyesno(
                "Update POTA Parks DB",
                "Download the latest POTA parks list from pota.app\n"
                "and rebuild the local database?\n\n"
                f"Destination: {PARKS_DB}"):
            return
        self._set_status("Downloading POTA parks list…")

        def _progress(msg):
            self.after(0, lambda: self._set_status(msg))

        def _worker():
            count, err = build_parks_db(progress_cb=_progress)
            if err:
                self.after(0, lambda: (
                    messagebox.showerror("Parks DB Error", err),
                    self._set_status(f"Parks DB update failed: {err}")))
            else:
                self.after(0, lambda: (
                    messagebox.showinfo(
                        "Parks DB Updated",
                        f"Downloaded and indexed {count:,} parks.\n"
                        f"Saved to: {PARKS_DB}"),
                    self._set_status(f"Parks DB updated — {count:,} parks.")))

        threading.Thread(target=_worker, daemon=True).start()

    def _update_fcc_db(self):
        fcc_date = self.cfg.get("fcc_db_date", "")
        action   = "re-download and update" if fcc_date else "download"
        if not messagebox.askyesno(
                "FCC Callsign Database",
                f"This will {action} the FCC amateur radio license database\n"
                "from data.fcc.gov (~175 MB download).\n\n"
                "After building, callsigns will auto-populate name, city,\n"
                "state, and license class when you tab out of the callsign field.\n\n"
                f"Destination: {FCC_DB}\n\nContinue?"):
            return
        self._set_status("Downloading FCC callsign database…")

        def _progress(msg):
            self.after(0, lambda: self._set_status(msg))

        def _worker():
            count, err = build_fcc_db(progress_cb=_progress)
            if err:
                self.after(0, lambda: (
                    messagebox.showerror("FCC DB Error", err),
                    self._set_status(f"FCC DB update failed: {err}")))
            else:
                today = datetime.date.today().isoformat()
                self.cfg["fcc_db_date"] = today
                save_config(self.cfg)
                self.after(0, lambda: (
                    messagebox.showinfo(
                        "FCC Database Ready",
                        f"Downloaded and indexed {count:,} US amateur callsigns.\n"
                        f"Saved to: {FCC_DB}"),
                    self._set_status(
                        f"FCC callsign DB updated — {count:,} callsigns.")))

        threading.Thread(target=_worker, daemon=True).start()

    def _check_parks_db_on_startup(self):
        if parks_db_exists():
            return
        if messagebox.askyesno(
                "POTA Parks Database",
                "The POTA parks database has not been built yet.\n\n"
                "Download it now? (~1 MB, runs in background)\n"
                f"Will be saved to: {PARKS_DB}"):
            self._update_parks_db()

    def _check_fcc_db_on_startup(self):
        fcc_date = self.cfg.get("fcc_db_date", "")
        if fcc_date:
            try:
                built = datetime.date.fromisoformat(fcc_date)
                age   = (datetime.date.today() - built).days
                if age > 30:
                    self._set_status(
                        f"FCC callsign DB is {age} days old — "
                        "consider updating via Settings > Update FCC Callsign DB…")
            except ValueError:
                pass

    def _switch_theme(self):
        current = self.cfg.get("theme", "dark")
        self.cfg["theme"] = "light" if current == "dark" else "dark"
        save_config(self.cfg)
        _apply_palette(self.cfg["theme"])
        self._rebuild_ui()

    def _rebuild_ui(self):
        for attr in ("_flrig_poll_id", "_pota_after_id", "_map_resize_id"):
            id_ = getattr(self, attr, None)
            if id_:
                self.after_cancel(id_)
        if getattr(self, "_search_after_id", None):
            self.after_cancel(self._search_after_id)

        adif_path = self.adif_path

        for w in self.winfo_children():
            w.destroy()

        self.configure(bg=BG)
        self._flrig_freq_hz  = None
        self._flrig_mode     = None
        self._flrig_poll_id  = None
        self._flrig_polling  = False
        self._tune_suppress_until = 0.0
        self._pota_paused    = False
        self._pota_loaded    = False
        self._pota_after_id  = None
        self._pota_spots_raw      = []
        self._pota_spots_filtered = []
        self._pota_band_var  = tk.StringVar(value=self.cfg.get("pota_band", "All"))
        self._pota_mode_var  = tk.StringVar(value=self.cfg.get("pota_mode", "All"))
        self._pota_hide_qrt  = tk.BooleanVar(value=self.cfg.get("pota_hide_qrt", False))
        self._pota_hide_oob  = tk.BooleanVar(value=self.cfg.get("pota_hide_oob", False))
        self._pota_itu_r1    = tk.BooleanVar(value=self.cfg.get("pota_itu_r1", True))
        self._pota_itu_r2    = tk.BooleanVar(value=self.cfg.get("pota_itu_r2", True))
        self._pota_itu_r3    = tk.BooleanVar(value=self.cfg.get("pota_itu_r3", True))
        self._pota_clicked_hz = None
        self._map_markers    = {}
        self._map_drawn      = False
        self._map_resize_id  = None
        self.adif_path       = ""

        self._style_ttk()
        self._build_menu()
        self._build_ui()

        if adif_path and os.path.exists(adif_path):
            self._open_adif(adif_path)

        if _qrz_session:
            self._qrz_lbl.config(text="QRZ: ✔", fg=ACC3)

        self._start_flrig_poll()

    # ── Flrig auto-start ──────────────────────────────────────────────────
    def _maybe_start_flrig(self):
        if not self.cfg.get("flrig_autostart") or not self.cfg.get("flrig_exe_path"):
            return
        freq, _ = flrig_get(self.cfg["flrig_host"], self.cfg["flrig_port"])
        if freq is not None:
            return  # already running — don't own it
        try:
            self._flrig_proc = subprocess.Popen(
                [self.cfg["flrig_exe_path"]],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._flrig_proc_owned = True
        except Exception as e:
            messagebox.showwarning("flrig", f"Could not start flrig:\n{e}")

    # ── Flrig poll ────────────────────────────────────────────────────────
    def _start_flrig_poll(self):
        self._flrig_polling = False
        self._do_flrig_poll()

    def _do_flrig_poll(self):
        if not self._flrig_polling:
            self._flrig_polling = True
            host = self.cfg["flrig_host"]
            port = self.cfg["flrig_port"]
            def _fetch():
                freq_hz, mode, smeter, pwrmeter, ptt = flrig_get_all(host, port)
                self._flrig_polling = False
                self.after(0, lambda: self._update_vfo_display(
                    freq_hz, mode, smeter=smeter, pwrmeter=pwrmeter, ptt=ptt))
            threading.Thread(target=_fetch, daemon=True).start()
        self._flrig_poll_id = self.after(2000, self._do_flrig_poll)

    def _update_vfo_display(self, freq_hz, mode, force=False,
                            smeter=None, pwrmeter=None, ptt=False):
        suppressed = not force and time.monotonic() < self._tune_suppress_until
        self._flrig_ptt = bool(ptt)
        if freq_hz is not None:
            if not suppressed:
                self._flrig_freq_hz = freq_hz
                self._flrig_mode    = mode
                try:
                    mhz = float(freq_hz) / 1_000_000
                except Exception:
                    mhz = float(freq_hz)
                band = freq_to_band(mhz)
                self._vfo_freq.config(text=f"{mhz:.4f} MHz", fg=ACCENT)
                self._vfo_mode.config(text=str(mode) if mode else "—", fg=ACC2)
                self._vfo_band.config(text=band if band else "—", fg=ACC3)
                self._refresh_pota_highlights()
            self._flrig_lbl.config(text="● Flrig: online", fg=ACC3)
            self._update_meter_display(smeter, pwrmeter, ptt)
        else:
            self._vfo_freq.config(text="—", fg=MUTED)
            self._vfo_mode.config(text="—", fg=MUTED)
            self._vfo_band.config(text="—", fg=MUTED)
            self._flrig_lbl.config(text="● Flrig: offline", fg=WARN)
            self._update_meter_display(None, None, False)

    def _update_meter_display(self, smeter, pwrmeter, ptt):
        is_tx = bool(ptt)
        if is_tx:
            raw = pwrmeter
            label = "PWR OUT"
        else:
            raw = smeter
            label = "S-METER"
        try:
            value = max(0, min(100, int(float(raw)))) if raw is not None else 0
        except (TypeError, ValueError):
            value = 0
        self._meter_value = value
        self._meter_is_tx = is_tx
        fg_color = WARN if is_tx else ACC3
        self._meter_type_lbl.config(text=label, fg=fg_color)
        self._meter_val_lbl.config(text=f"{value:3d}%", fg=fg_color)
        self._draw_meter_bar(value, is_tx)

    def _draw_meter_bar(self, value, is_tx):
        c = self._meter_canvas
        c.delete("all")
        w = c.winfo_width()
        if w < 10:
            return
        h = c.winfo_height()
        n_segs = 20
        gap = 2
        seg_w = (w - gap * (n_segs - 1)) / n_segs
        lit_count = round(value / 100 * n_segs)
        if is_tx:
            lit_color = "#dd3333"
            dim_color = "#2a0808"
        else:
            lit_color = "#22bb55"
            dim_color = "#062010"
        for i in range(n_segs):
            x1 = i * (seg_w + gap)
            x2 = x1 + seg_w
            color = lit_color if i < lit_count else dim_color
            c.create_rectangle(x1, 1, x2, h - 1, fill=color, outline="")

    # ── QRZ ───────────────────────────────────────────────────────────────
    def _qrz_login_bg(self):
        result = qrz_login(self.cfg["qrz_user"], self.cfg["qrz_pass"])
        if result is True:
            self.after(0, lambda: self._qrz_lbl.config(text="QRZ: ✔", fg=ACC3))
        else:
            self.after(0, lambda: self._qrz_lbl.config(text="QRZ: ✗", fg=WARN))

    # ── Misc ──────────────────────────────────────────────────────────────
    def _set_status(self, msg):
        self._status_var.set(msg)

    def _warn_out_of_band(self, freq_mhz):
        lic = self.cfg.get("license_class", "?")
        messagebox.showwarning(
            "Out of Band",
            f"{freq_mhz:.4f} MHz is outside your {lic} license privileges.\n\nOut of Band",
            parent=self)

    def _update_mycall_lbl(self):
        call = self.cfg.get("callsign","")
        self._mycall_lbl.config(text=call.upper() if call else "No callsign set")

    def _about(self):
        messagebox.showinfo("About POTA Hunter",
            "POTA Hunter v2.0 — POTA Activator Hunter & Ham Radio Logger\n\n"
            "Entry order:\n"
            "  Callsign → RST Sent → RST Rcvd\n"
            "  → Park # → Comments → Notes\n\n"
            "Date/Time  : UTC-stamped at moment of LOG QSO\n"
            "Freq/Band/Mode : captured from Flrig at LOG QSO\n"
            "Storage    : native ADIF (.adi) per logbook\n\n"
            "Logbooks folder: " + LOGBOOK_DIR)

    # ── Voice Keyer ───────────────────────────────────────────────────────
    def _open_voice_keyer(self):
        if self._vk_window and self._vk_window.winfo_exists():
            self._vk_window.lift()
            return
        self._vk_window = VoiceKeyerWindow(self)

    def _start_vk_hotkey_listener(self):
        if not _PYNPUT_OK:
            return
        _fkey_map = {
            _pynput_kb.Key.f1: "F1", _pynput_kb.Key.f2: "F2",
            _pynput_kb.Key.f3: "F3", _pynput_kb.Key.f4: "F4",
            _pynput_kb.Key.f5: "F5", _pynput_kb.Key.f6: "F6",
            _pynput_kb.Key.f7: "F7", _pynput_kb.Key.f8: "F8",
            _pynput_kb.Key.f9: "F9",
        }
        def _on_press(key):
            label = _fkey_map.get(key)
            if label and self._vk_window and self._vk_window.winfo_exists():
                self.after(0, lambda k=label: self._vk_window.play(k))
        try:
            listener = _pynput_kb.Listener(on_press=_on_press)
            listener.daemon = True
            listener.start()
            self._vk_listener = listener
        except Exception:
            pass

    def destroy(self):
        if self._flrig_proc_owned and self._flrig_proc and self._flrig_proc.poll() is None:
            self._flrig_proc.terminate()
        if self._flrig_poll_id:
            self.after_cancel(self._flrig_poll_id)
        if self._pota_after_id:
            self.after_cancel(self._pota_after_id)
        if self._pota_scan_after_id:
            self.after_cancel(self._pota_scan_after_id)
        if self._map_resize_id:
            self.after_cancel(self._map_resize_id)
        if self._map_server:
            threading.Thread(target=self._map_server.shutdown, daemon=True).start()
        if self.conn:
            self.conn.close()
        if self._vk_listener:
            try:
                self._vk_listener.stop()
            except Exception:
                pass
        if self._vk_window:
            try:
                self._vk_window.destroy()
            except Exception:
                pass
        save_config(self.cfg)
        super().destroy()


# ══════════════════════════════════════════════════════════════════════════════
class VoiceKeyerSettingsDialog(tk.Toplevel):
    """Configure audio devices and PTT options for the voice keyer."""
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("Voice Keyer Settings")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.cfg = cfg
        ent = dict(bg=BG3, fg=FG, font=MONO, relief="flat", insertbackground=ACCENT, bd=4)
        lbl = dict(bg=BG, fg=FG2, font=SM)

        self._ptt_var = tk.BooleanVar(value=cfg.get("vk_ptt_enabled", False))
        tk.Checkbutton(self, text="Enable PTT via Flrig during playback",
                       variable=self._ptt_var, bg=BG, fg=FG2, selectcolor=BG3,
                       activebackground=BG, activeforeground=FG,
                       font=SM).grid(row=0, column=0, columnspan=2,
                                     sticky="w", padx=12, pady=(12, 4))

        tk.Label(self, text="PTT key-up delay (ms):", **lbl).grid(
            row=1, column=0, sticky="e", padx=(12, 6), pady=4)
        self._e_delay = tk.Entry(self, width=8, **ent)
        self._e_delay.insert(0, str(cfg.get("vk_ptt_delay_ms", 200)))
        self._e_delay.grid(row=1, column=1, sticky="w", padx=(0, 12), pady=4)

        devices = self._get_devices()
        in_devs  = [""] + [d["name"] for d in devices if d["max_input_channels"]  > 0]
        out_devs = [""] + [d["name"] for d in devices if d["max_output_channels"] > 0]

        tk.Label(self, text="Input device:", **lbl).grid(
            row=2, column=0, sticky="e", padx=(12, 6), pady=4)
        self._in_var = tk.StringVar(value=cfg.get("vk_input_device", ""))
        ttk.Combobox(self, textvariable=self._in_var, values=in_devs,
                     width=32, state="readonly").grid(
            row=2, column=1, sticky="w", padx=(0, 12), pady=4)

        tk.Label(self, text="Output device:", **lbl).grid(
            row=3, column=0, sticky="e", padx=(12, 6), pady=4)
        self._out_var = tk.StringVar(value=cfg.get("vk_output_device", ""))
        ttk.Combobox(self, textvariable=self._out_var, values=out_devs,
                     width=32, state="readonly").grid(
            row=3, column=1, sticky="w", padx=(0, 12), pady=4)

        if not _AUDIO_OK:
            tk.Label(self,
                     text="⚠ sounddevice not installed\n"
                          "Run: pip install sounddevice numpy",
                     bg=BG, fg=WARN, font=SM, justify="left").grid(
                row=4, column=0, columnspan=2, padx=12, pady=4, sticky="w")

        bf = tk.Frame(self, bg=BG)
        bf.grid(row=5, column=0, columnspan=2, pady=10)
        bc = dict(font=SM, relief="flat", cursor="hand2", pady=4, padx=12)
        tk.Button(bf, text="✔ Save",   bg=ACC3, fg=BG, command=self._save, **bc).pack(side="left", padx=6)
        tk.Button(bf, text="✕ Cancel", bg=BG3,  fg=FG, command=self.destroy, **bc).pack(side="left")

    def _get_devices(self):
        if not _AUDIO_OK:
            return []
        try:
            return list(sd.query_devices())
        except Exception:
            return []

    def _save(self):
        self.cfg["vk_ptt_enabled"] = self._ptt_var.get()
        try:
            self.cfg["vk_ptt_delay_ms"] = int(self._e_delay.get().strip())
        except ValueError:
            pass
        self.cfg["vk_input_device"]  = self._in_var.get()
        self.cfg["vk_output_device"] = self._out_var.get()
        save_config(self.cfg)
        self.destroy()


class VoiceKeyerWindow(tk.Toplevel):
    """Floating voice keyer — pre-record F1-F9 messages and play via hotkey."""

    _KEYS = [f"F{i}" for i in range(1, 10)]

    def __init__(self, parent):
        super().__init__(parent)
        self.title("Voice Keyer")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self._app = parent
        self._recording_key = None
        self._playing_key   = None
        self._stop_event    = threading.Event()

        self._lbl_widgets    = {}
        self._status_widgets = {}
        self._rec_btns       = {}
        self._play_btns      = {}

        self._build_ui()
        self._update_indicators()

    # ── paths ──────────────────────────────────────────────────────────────
    def _wav_path(self, key):
        return os.path.join(self._app._vk_dir, f"{key}.wav")

    def _has_recording(self, key):
        return os.path.exists(self._wav_path(key))

    # ── UI ─────────────────────────────────────────────────────────────────
    def _build_ui(self):
        ent = dict(bg=BG3, fg=FG, font=SM, relief="flat",
                   insertbackground=ACCENT, bd=2)
        labels = self._app.cfg.get("vk_labels", {})

        for i, key in enumerate(self._KEYS):
            tk.Label(self, text=key, width=3, bg=BG, fg=ACCENT,
                     font=LBL, anchor="e").grid(
                row=i, column=0, padx=(8, 4), pady=2, sticky="e")

            e = tk.Entry(self, width=22, **ent)
            e.insert(0, labels.get(key, ""))
            e.grid(row=i, column=1, padx=(0, 4), pady=2)
            e.bind("<FocusOut>", lambda ev, k=key, w=e: self._save_label(k, w))
            self._lbl_widgets[key] = e

            s = tk.Label(self, text="○", width=2, bg=BG, fg=FG2, font=SM)
            s.grid(row=i, column=2, padx=2, pady=2)
            self._status_widgets[key] = s

            rb = tk.Button(self, text="● Rec", bg=BG3, fg=WARN, font=SM,
                           relief="flat", cursor="hand2", padx=6,
                           command=lambda k=key: self._start_record(k))
            rb.grid(row=i, column=3, padx=2, pady=2)
            self._rec_btns[key] = rb

            pb = tk.Button(self, text="▶ Play", bg=BG3, fg=ACC3, font=SM,
                           relief="flat", cursor="hand2", padx=6,
                           command=lambda k=key: self.play(k))
            pb.grid(row=i, column=4, padx=2, pady=2)
            self._play_btns[key] = pb

            tk.Button(self, text="✕", bg=BG3, fg=FG2, font=SM,
                      relief="flat", cursor="hand2", padx=4,
                      command=lambda k=key: self._clear(k)).grid(
                row=i, column=5, padx=(2, 8), pady=2)

        bot = tk.Frame(self, bg=BG)
        bot.grid(row=len(self._KEYS), column=0, columnspan=6, pady=8)
        bc = dict(font=SM, relief="flat", cursor="hand2", padx=8, pady=3)
        tk.Button(bot, text="■ Stop", bg=BG4, fg=WARN,
                  command=self._stop, **bc).pack(side="left", padx=6)
        self._status_lbl = tk.Label(bot, text="Ready", bg=BG, fg=FG2, font=SM)
        self._status_lbl.pack(side="left", padx=6)
        tk.Button(bot, text="⚙ Settings", bg=BG4, fg=FG,
                  command=self._open_settings, **bc).pack(side="right", padx=6)

        if not _AUDIO_OK:
            tk.Label(self,
                     text="⚠ sounddevice not installed — run: pip install sounddevice numpy",
                     bg=BG, fg=WARN, font=SM).grid(
                row=len(self._KEYS) + 1, column=0, columnspan=6, pady=(0, 6))

    # ── helpers ────────────────────────────────────────────────────────────
    def _save_label(self, key, widget):
        self._app.cfg.setdefault("vk_labels", {})[key] = widget.get()
        save_config(self._app.cfg)

    def _update_indicators(self):
        for key in self._KEYS:
            if self._has_recording(key):
                self._status_widgets[key].config(text="●", fg=ACC3)
            else:
                self._status_widgets[key].config(text="○", fg=FG2)

    def _set_status(self, text, color=None):
        self.after(0, lambda: self._status_lbl.config(
            text=text, fg=color if color else FG2))

    # ── record ─────────────────────────────────────────────────────────────
    def _start_record(self, key):
        if not _AUDIO_OK:
            messagebox.showwarning(
                "No Audio",
                "sounddevice is not installed.\nRun: pip install sounddevice numpy",
                parent=self)
            return
        if self._recording_key or self._playing_key:
            return
        self._recording_key = key
        self._stop_event.clear()
        self._set_status(f"Recording {key}…", WARN)
        self._rec_btns[key].config(bg=WARN, fg=BG)
        threading.Thread(target=self._record_thread,
                         args=(key,), daemon=True).start()

    def _record_thread(self, key):
        path = self._wav_path(key)
        samplerate = 44100
        dev = self._resolve_device("vk_input_device", input=True)
        frames = []
        try:
            with sd.InputStream(samplerate=samplerate, channels=1,
                                device=dev, dtype="int16") as stream:
                while not self._stop_event.is_set():
                    data, _ = stream.read(1024)
                    frames.append(data.copy())
        except Exception as exc:
            self.after(0, lambda: self._set_status(f"Rec error: {exc}", WARN))
            self._recording_key = None
            self.after(0, lambda k=key: self._rec_btns[k].config(bg=BG3, fg=WARN))
            return

        if frames:
            audio = np.concatenate(frames)
            try:
                with wave.open(path, "w") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(samplerate)
                    wf.writeframes(audio.tobytes())
            except Exception as exc:
                self.after(0, lambda: self._set_status(f"Save error: {exc}", WARN))

        self._recording_key = None
        def _done(k=key):
            self._rec_btns[k].config(bg=BG3, fg=WARN)
            self._update_indicators()
            self._status_lbl.config(text="Ready", fg=FG2)
        self.after(0, _done)

    # ── play ───────────────────────────────────────────────────────────────
    def play(self, key):
        if not _AUDIO_OK:
            messagebox.showwarning(
                "No Audio",
                "sounddevice is not installed.\nRun: pip install sounddevice numpy",
                parent=self)
            return
        if self._playing_key or self._recording_key:
            return
        path = self._wav_path(key)
        if not os.path.exists(path):
            self._set_status(f"No recording for {key}", WARN)
            return
        self._playing_key = key
        self._stop_event.clear()
        self._set_status(f"TX {key}…", ACC3)
        self._play_btns[key].config(bg=ACC3, fg=BG)
        threading.Thread(target=self._play_thread,
                         args=(key, path), daemon=True).start()

    def _play_thread(self, key, path):
        host    = self._app.cfg.get("flrig_host", "127.0.0.1")
        port    = self._app.cfg.get("flrig_port", 12345)
        ptt_en  = self._app.cfg.get("vk_ptt_enabled", False)
        delay   = self._app.cfg.get("vk_ptt_delay_ms", 200) / 1000.0
        dev     = self._resolve_device("vk_output_device", input=False)

        if ptt_en:
            flrig_set_ptt(host, port, True)
            time.sleep(delay)
        try:
            with wave.open(path) as wf:
                raw  = wf.readframes(wf.getnframes())
                rate = wf.getframerate()
                ch   = wf.getnchannels()
                sw   = wf.getsampwidth()
            dtype = {1: np.int8, 2: np.int16, 4: np.int32}.get(sw, np.int16)
            audio = np.frombuffer(raw, dtype=dtype)
            if ch > 1:
                audio = audio.reshape(-1, ch)
            sd.play(audio, samplerate=rate, device=dev, blocking=True)
        except Exception as exc:
            self.after(0, lambda: self._set_status(f"Play error: {exc}", WARN))
        finally:
            if ptt_en:
                flrig_set_ptt(host, port, False)
            self._playing_key = None
            def _done(k=key):
                self._play_btns[k].config(bg=BG3, fg=ACC3)
                self._status_lbl.config(text="Ready", fg=FG2)
            self.after(0, _done)

    # ── stop ───────────────────────────────────────────────────────────────
    def _stop(self):
        self._stop_event.set()
        if _AUDIO_OK:
            try:
                sd.stop()
            except Exception:
                pass

    # ── clear ──────────────────────────────────────────────────────────────
    def _clear(self, key):
        path = self._wav_path(key)
        if not os.path.exists(path):
            return
        if messagebox.askyesno("Clear Recording",
                               f"Delete the recording for {key}?", parent=self):
            try:
                os.remove(path)
            except Exception:
                pass
            self._update_indicators()

    # ── device resolution ──────────────────────────────────────────────────
    def _resolve_device(self, cfg_key, input=True):
        name = self._app.cfg.get(cfg_key, "")
        if not name or not _AUDIO_OK:
            return None
        try:
            key_ch = "max_input_channels" if input else "max_output_channels"
            for i, d in enumerate(sd.query_devices()):
                if d["name"] == name and d[key_ch] > 0:
                    return i
        except Exception:
            pass
        return None

    # ── settings ───────────────────────────────────────────────────────────
    def _open_settings(self):
        VoiceKeyerSettingsDialog(self, self._app.cfg)

    def _on_close(self):
        self._stop()
        self._app._vk_window = None
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
class EditDialog(tk.Toplevel):
    """Edit an existing QSO — rewrites ADIF on save."""
    def __init__(self, parent, conn, adif_path, mycall, row, refresh_cb):
        super().__init__(parent)
        self.title(f"Edit QSO #{row['id']} — {row['call']}")
        self.configure(bg=BG)
        self.resizable(False, False)
        self.grab_set()
        self.conn       = conn
        self.adif_path  = adif_path
        self.mycall     = mycall
        self.row        = row
        self.refresh_cb = refresh_cb

        ent = dict(bg=BG3, fg=FG, font=MONO, relief="flat",
                   insertbackground=ACCENT, bd=4)
        lbl = dict(bg=BG, fg=FG2, font=SM)

        fields = [
            ("Callsign",          "call",        None),
            ("Date (YYYY-MM-DD)", "date",        None),
            ("Time UTC (HHMM)",   "time_on",     None),
            ("Freq (MHz)",        "freq",        None),
            ("Band",              "band",        "band"),
            ("Mode",              "mode",        "mode"),
            ("RST Sent",          "rst_sent",    None),
            ("RST Rcvd",          "rst_rcvd",    None),
            ("Name",              "name",        None),
            ("QTH",               "qth",         None),
            ("Grid Square",       "gridsquare",  None),
            ("Park #",            "park_nr",     None),
            ("Comments",          "comment",     None),
            ("Notes",             "notes",       None),
        ]
        self._entries = {}
        for i, (label, key, combo) in enumerate(fields):
            tk.Label(self, text=label, **lbl).grid(
                row=i, column=0, sticky="e", padx=(12,6), pady=3)
            if combo == "mode":
                w = ttk.Combobox(self, values=MODES, width=20, font=SM)
                w.set(row[key] or "")
            elif combo == "band":
                w = ttk.Combobox(self, values=BANDS, width=20, font=SM)
                w.set(row[key] or "")
            else:
                w = tk.Entry(self, width=26, **ent)
                val = row[key]
                if val is not None:
                    w.insert(0, str(val))
            w.grid(row=i, column=1, padx=(0,12), pady=3, sticky="w")
            self._entries[key] = w

        bf = tk.Frame(self, bg=BG)
        bf.grid(row=len(fields), column=0, columnspan=2, pady=10)
        bc = dict(font=SM, relief="flat", cursor="hand2", pady=4, padx=12)
        tk.Button(bf, text="✔ Save", bg=ACC3, fg=BG,
                  command=self._save, **bc).pack(side="left", padx=6)
        tk.Button(bf, text="✕ Cancel", bg=BG3, fg=FG,
                  command=self.destroy, **bc).pack(side="left", padx=6)
        tk.Button(bf, text="🗑 Delete", bg=WARN, fg=BG,
                  command=self._delete, **bc).pack(side="left")

    def _delete(self):
        if not messagebox.askyesno("Delete",
                f"Delete QSO #{self.row['id']} with {self.row['call']} on {self.row['date']}?",
                parent=self):
            return
        self.conn.execute("DELETE FROM qso WHERE id=?", (self.row["id"],))
        self.conn.commit()
        rewrite_adif(self.adif_path, self.conn, self.mycall.upper())
        load_adif_into_index(self.adif_path, self.conn)
        self.refresh_cb()
        self.destroy()

    def _save(self):
        vals = {k: w.get().strip() for k, w in self._entries.items()}
        try:
            vals["freq"] = float(vals["freq"]) if vals["freq"] else None
        except ValueError:
            messagebox.showerror("Invalid","Frequency must be a number.",parent=self)
            return
        self.conn.execute("""
            UPDATE qso SET call=?,date=?,time_on=?,freq=?,band=?,mode=?,
            rst_sent=?,rst_rcvd=?,name=?,qth=?,gridsquare=?,park_nr=?,
            comment=?,notes=? WHERE id=?""",
            (vals["call"].upper(), vals["date"], vals["time_on"],
             vals["freq"], vals["band"], vals["mode"],
             vals["rst_sent"], vals["rst_rcvd"], vals["name"],
             vals["qth"], vals["gridsquare"], vals["park_nr"],
             vals["comment"], vals["notes"], self.row["id"]))
        self.conn.commit()
        rewrite_adif(self.adif_path, self.conn, self.mycall.upper())
        self.refresh_cb()
        self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
class StationDialog(tk.Toplevel):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("Station Settings"); self.configure(bg=BG)
        self.resizable(False,False); self.grab_set()
        self.cfg=cfg; self.parent=parent
        ent=dict(bg=BG3,fg=FG,font=MONO,relief="flat",insertbackground=ACCENT,bd=4)
        lbl=dict(bg=BG,fg=FG2,font=SM)
        fields=[("My Callsign","callsign"),("Grid Square","gridsquare")]
        self._entries={}
        for i,(label,key) in enumerate(fields):
            tk.Label(self,text=label,**lbl).grid(row=i,column=0,
                sticky="e",padx=(12,6),pady=6)
            w=tk.Entry(self,width=20,**ent); w.insert(0,cfg.get(key,""))
            w.grid(row=i,column=1,padx=(0,12),pady=6); self._entries[key]=w
        tk.Label(self,text="License Class",**lbl).grid(row=len(fields),column=0,
            sticky="e",padx=(12,6),pady=6)
        self._lic_cb=ttk.Combobox(self,values=["Technician","General","Extra"],
            state="readonly",width=18,font=MONO)
        self._lic_cb.set(cfg.get("license_class","Extra"))
        self._lic_cb.grid(row=len(fields),column=1,padx=(0,12),pady=6)
        bf=tk.Frame(self,bg=BG); bf.grid(row=len(fields)+1,column=0,columnspan=2,pady=10)
        bc=dict(font=SM,relief="flat",cursor="hand2",pady=4,padx=12)
        tk.Button(bf,text="✔ Save",bg=ACC3,fg=BG,command=self._save,**bc).pack(side="left",padx=6)
        tk.Button(bf,text="✕ Cancel",bg=BG3,fg=FG,command=self.destroy,**bc).pack(side="left")
    def _save(self):
        for key,w in self._entries.items():
            self.cfg[key]=w.get().strip().upper()
        self.cfg["license_class"]=self._lic_cb.get()
        save_config(self.cfg); self.parent._update_mycall_lbl(); self.destroy()


# ══════════════════════════════════════════════════════════════════════════════
class QRZDialog(tk.Toplevel):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("QRZ Login"); self.configure(bg=BG)
        self.resizable(False,False); self.grab_set()
        self.cfg=cfg; self.parent=parent
        ent=dict(bg=BG3,fg=FG,font=MONO,relief="flat",insertbackground=ACCENT,bd=4)
        lbl=dict(bg=BG,fg=FG2,font=SM)
        tk.Label(self,text="QRZ.com Username:",**lbl).grid(row=0,column=0,sticky="e",padx=(12,6),pady=6)
        self.e_user=tk.Entry(self,width=20,**ent); self.e_user.insert(0,cfg.get("qrz_user",""))
        self.e_user.grid(row=0,column=1,padx=(0,12),pady=6)
        tk.Label(self,text="QRZ.com Password:",**lbl).grid(row=1,column=0,sticky="e",padx=(12,6),pady=6)
        self.e_pass=tk.Entry(self,width=20,show="*",**ent); self.e_pass.insert(0,cfg.get("qrz_pass",""))
        self.e_pass.grid(row=1,column=1,padx=(0,12),pady=6)
        self._res=tk.Label(self,text="",bg=BG,fg=ACC3,font=SM)
        self._res.grid(row=2,column=0,columnspan=2)
        bf=tk.Frame(self,bg=BG); bf.grid(row=3,column=0,columnspan=2,pady=10)
        bc=dict(font=SM,relief="flat",cursor="hand2",pady=4,padx=12)
        tk.Button(bf,text="✔ Login & Save",bg=ACC3,fg=BG,command=self._login,**bc).pack(side="left",padx=6)
        tk.Button(bf,text="✕ Cancel",bg=BG3,fg=FG,command=self.destroy,**bc).pack(side="left")
    def _login(self):
        user=self.e_user.get().strip(); pw=self.e_pass.get().strip()
        self._res.config(text="Logging in…",fg=FG2)
        self.cfg["qrz_user"]=user; self.cfg["qrz_pass"]=pw; save_config(self.cfg)
        def bg():
            result=qrz_login(user,pw)
            if result is True:
                self.after(0,lambda:self._res.config(text="✔ Login successful!",fg=ACC3))
                self.after(0,lambda:self.parent._qrz_lbl.config(text="QRZ: ✔",fg=ACC3))
            else:
                self.after(0,lambda:self._res.config(text=f"✗ {result}",fg=WARN))
                self.after(0,lambda:self.parent._qrz_lbl.config(text="QRZ: ✗",fg=WARN))
        threading.Thread(target=bg,daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
class FlrigDialog(tk.Toplevel):
    def __init__(self, parent, cfg):
        super().__init__(parent)
        self.title("Flrig Settings"); self.configure(bg=BG)
        self.resizable(False,False); self.grab_set()
        self.cfg=cfg; self.parent=parent
        ent=dict(bg=BG3,fg=FG,font=MONO,relief="flat",insertbackground=ACCENT,bd=4)
        lbl=dict(bg=BG,fg=FG2,font=SM)
        tk.Label(self,text="Flrig Host:",**lbl).grid(row=0,column=0,sticky="e",padx=(12,6),pady=6)
        self.e_host=tk.Entry(self,width=20,**ent); self.e_host.insert(0,cfg.get("flrig_host","127.0.0.1"))
        self.e_host.grid(row=0,column=1,columnspan=2,padx=(0,12),pady=6,sticky="w")
        tk.Label(self,text="Flrig Port:",**lbl).grid(row=1,column=0,sticky="e",padx=(12,6),pady=6)
        self.e_port=tk.Entry(self,width=8,**ent); self.e_port.insert(0,str(cfg.get("flrig_port",12345)))
        self.e_port.grid(row=1,column=1,columnspan=2,padx=(0,12),pady=6,sticky="w")
        # Auto-start option
        self._autostart_var=tk.BooleanVar(value=cfg.get("flrig_autostart",False))
        tk.Checkbutton(self,text="Auto-start flrig on launch / close on exit",
                       variable=self._autostart_var,bg=BG,fg=FG2,selectcolor=BG3,
                       activebackground=BG,activeforeground=FG,font=SM,
                       command=self._toggle_path_state
                       ).grid(row=2,column=0,columnspan=3,sticky="w",padx=(12,6),pady=(8,2))
        tk.Label(self,text="Flrig Path:",**lbl).grid(row=3,column=0,sticky="e",padx=(12,6),pady=4)
        self.e_path=tk.Entry(self,width=28,**ent); self.e_path.insert(0,cfg.get("flrig_exe_path",""))
        self.e_path.grid(row=3,column=1,padx=(0,4),pady=4,sticky="w")
        self._browse_btn=tk.Button(self,text="Browse…",bg=BG4,fg=FG,font=SM,relief="flat",
                                   cursor="hand2",padx=6,pady=2,command=self._browse)
        self._browse_btn.grid(row=3,column=2,padx=(0,12),pady=4,sticky="w")
        self._toggle_path_state()
        self._tl=tk.Label(self,text="",bg=BG,fg=FG2,font=SM)
        self._tl.grid(row=4,column=0,columnspan=3)
        bf=tk.Frame(self,bg=BG); bf.grid(row=5,column=0,columnspan=3,pady=10)
        bc=dict(font=SM,relief="flat",cursor="hand2",pady=4,padx=12)
        tk.Button(bf,text="⟳ Test",bg=BG4,fg=FG,command=self._test,**bc).pack(side="left",padx=6)
        tk.Button(bf,text="✔ Save",bg=ACC3,fg=BG,command=self._save,**bc).pack(side="left",padx=6)
        tk.Button(bf,text="✕ Cancel",bg=BG3,fg=FG,command=self.destroy,**bc).pack(side="left")
    def _toggle_path_state(self):
        state = "normal" if self._autostart_var.get() else "disabled"
        self.e_path.config(state=state)
        self._browse_btn.config(state=state)
    def _browse(self):
        import sys
        filetypes = [("Executable", "*.exe"), ("All files", "*.*")] if sys.platform == "win32" \
                    else [("All files", "*.*")]
        path = filedialog.askopenfilename(title="Select flrig executable", filetypes=filetypes)
        if path:
            self.e_path.delete(0, "end")
            self.e_path.insert(0, path)
    def _test(self):
        host=self.e_host.get().strip()
        try: port=int(self.e_port.get().strip())
        except ValueError: self._tl.config(text="Invalid port",fg=WARN); return
        freq,mode=flrig_get(host,port)
        if freq is not None:
            try: mhz=float(freq)/1_000_000
            except Exception: mhz=freq
            self._tl.config(text=f"✔ Connected — {mhz:.4f} MHz  {mode}",fg=ACC3)
        else:
            self._tl.config(text="✗ Cannot reach Flrig",fg=WARN)
    def _save(self):
        self.cfg["flrig_host"]=self.e_host.get().strip()
        try: self.cfg["flrig_port"]=int(self.e_port.get().strip())
        except ValueError: pass
        self.cfg["flrig_autostart"]=self._autostart_var.get()
        self.cfg["flrig_exe_path"]=self.e_path.get().strip()
        save_config(self.cfg); self.destroy()


# ── Entry point ───────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _apply_palette(load_config().get("theme", "dark"))
    app = POTAHunter()
    app.mainloop()
