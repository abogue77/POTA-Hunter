#!/usr/bin/env python3
"""
POTA Hunter TUI — Parks on the Air Activator Hunter & Ham Radio Logger
Terminal UI, curses-based, stdlib only.  Compatible with potahunter.pyw
config and logbook files.
"""

# ── Section 1: Imports & locale ───────────────────────────────────────────────
import curses
import curses.panel
import locale
import threading
import queue
import time
import datetime
import sqlite3
import json
import csv
import zipfile
import io
import re
import pathlib
import dataclasses
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
import xmlrpc.client
from typing import Optional, List, Dict, Any

locale.setlocale(locale.LC_ALL, "")
_UTF8 = locale.getpreferredencoding(False).upper() in ("UTF-8", "UTF8")

# Box-drawing characters — Unicode if terminal supports it, ASCII fallback
if _UTF8:
    B_TL, B_TR, B_BL, B_BR = "╔", "╗", "╚", "╝"
    B_H, B_V = "═", "║"
    B_LT, B_RT, B_TT, B_BT, B_CR = "╠", "╣", "╦", "╩", "╬"
    ARROW = "▶"
    BLOCK_FULL, BLOCK_LIGHT = "█", "░"
    ELLIPSIS = "…"
else:
    B_TL, B_TR, B_BL, B_BR = "+", "+", "+", "+"
    B_H, B_V = "-", "|"
    B_LT, B_RT, B_TT, B_BT, B_CR = "+", "+", "+", "+", "+"
    ARROW = ">"
    BLOCK_FULL, BLOCK_LIGHT = "#", "."
    ELLIPSIS = "~"

# ── Section 2: Backend utilities (ported verbatim from potahunter.pyw) ────────

LOGBOOK_DIR   = pathlib.Path.home() / "POTA-Hunter"
CONFIG_FILE   = LOGBOOK_DIR / "config.json"
PARKS_DB      = LOGBOOK_DIR / "pota_parks.db"
FCC_DB        = LOGBOOK_DIR / "fcc_calls.db"
PARKS_CSV_URL = "https://pota.app/all_parks_ext.csv"
FCC_ZIP_URLS  = [
    "https://data.fcc.gov/download/pub/uls/complete/l_amat.zip",
    "http://www.nc7j.com/downloads/AR6/fcc/complete/l_amat.zip",
]

LOGBOOK_DIR.mkdir(exist_ok=True)

DEFAULT_CONFIG = {
    "callsign":              "",
    "gridsquare":            "",
    "qrz_user":              "",
    "qrz_pass":              "",
    "flrig_host":            "127.0.0.1",
    "flrig_port":            12345,
    "last_logbook":          "",
    "theme":                 "dark",
    "pota_band":             "All",
    "pota_mode":             "All",
    "pota_hide_qrt":         False,
    "pota_hide_oob":         False,
    "pota_itu_r1":           True,
    "pota_itu_r2":           True,
    "pota_itu_r3":           True,
    "pota_scan_skip_worked": False,
    "pota_scan_interval":    30,
    "fcc_db_date":           "",
    "license_class":         "Extra",
    "my_park":               "",
    "flrig_poll_interval":   1.5,
    "spot_refresh_interval": 120,
}

def load_config() -> dict:
    if CONFIG_FILE.exists():
        try:
            with open(CONFIG_FILE) as f:
                cfg = DEFAULT_CONFIG.copy()
                cfg.update(json.load(f))
                return cfg
        except Exception:
            pass
    return DEFAULT_CONFIG.copy()

def save_config(cfg: dict):
    with open(CONFIG_FILE, "w") as f:
        json.dump(cfg, f, indent=2)

# ── SQLite in-memory index ────────────────────────────────────────────────────
def make_index() -> sqlite3.Connection:
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

# ── ADIF helpers ──────────────────────────────────────────────────────────────
def adif_field(tag, val):
    if val is None or str(val).strip() == "":
        return ""
    v = str(val).strip()
    return f"<{tag.upper()}:{len(v)}>{v} "

def adif_header(mycall=""):
    now = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M:%SZ")
    return (f"POTA Hunter TUI ADIF Log — {now}  Station: {mycall}\n"
            + adif_field("ADIF_VER", "3.1.0")
            + adif_field("PROGRAMID", "POTA Hunter TUI")
            + "<EOH>\n\n")

def row_to_adif(row, mycall=""):
    r = dict(row)
    date_str  = r.get("date", "").replace("-", "")
    freq_val  = r.get("freq")
    freq_str  = f"{float(freq_val):.6g}" if freq_val else ""
    return (
        adif_field("CALL",             r.get("call", "")) +
        adif_field("QSO_DATE",         date_str) +
        adif_field("TIME_ON",          r.get("time_on", "")) +
        adif_field("FREQ",             freq_str) +
        adif_field("BAND",             r.get("band", "")) +
        adif_field("MODE",             r.get("mode", "")) +
        adif_field("RST_SENT",         r.get("rst_sent", "")) +
        adif_field("RST_RCVD",         r.get("rst_rcvd", "")) +
        adif_field("NAME",             r.get("name", "")) +
        adif_field("QTH",              r.get("qth", "")) +
        adif_field("GRIDSQUARE",       r.get("gridsquare", "")) +
        adif_field("POTA_REF",         r.get("park_nr", "")) +
        adif_field("COMMENT",          r.get("comment", "")) +
        adif_field("NOTES",            r.get("notes", "")) +
        adif_field("STATION_CALLSIGN", mycall)
    ).strip() + " <EOR>\n"

def parse_adif_records(text: str) -> list:
    eoh = text.upper().find("<EOH>")
    if eoh >= 0:
        text = text[eoh + 5:]
    records = []
    for rec in re.split(r"<EOR>", text, flags=re.IGNORECASE):
        rec = rec.strip()
        if not rec:
            continue
        d = {}
        for m in re.finditer(r"<(\w+):\d+(?::\w+)?>([^<]*)", rec, re.DOTALL):
            d[m.group(1).upper()] = m.group(2).strip()
        if d.get("CALL", "").strip():
            records.append(d)
    return records

def adif_to_row_dict(d: dict) -> dict:
    qso_date = d.get("QSO_DATE", "")
    if len(qso_date) == 8:
        qso_date = f"{qso_date[:4]}-{qso_date[4:6]}-{qso_date[6:]}"
    freq_str = d.get("FREQ", "")
    try:
        freq = float(freq_str) if freq_str else None
    except ValueError:
        freq = None
    band = d.get("BAND", "") or (freq_to_band(freq) if freq else "")
    return {
        "call":       d.get("CALL", "").upper(),
        "date":       qso_date,
        "time_on":    d.get("TIME_ON", ""),
        "freq":       freq,
        "band":       band,
        "mode":       d.get("MODE", ""),
        "rst_sent":   d.get("RST_SENT", "59"),
        "rst_rcvd":   d.get("RST_RCVD", "59"),
        "name":       d.get("NAME", ""),
        "qth":        d.get("QTH", ""),
        "gridsquare": d.get("GRIDSQUARE", "") or d.get("GRID", ""),
        "park_nr":    d.get("POTA_REF", ""),
        "comment":    d.get("COMMENT", ""),
        "notes":      d.get("NOTES", ""),
    }

def load_adif_into_index(adif_path, conn: sqlite3.Connection) -> int:
    conn.execute("DELETE FROM qso")
    conn.execute("DELETE FROM sqlite_sequence WHERE name='qso'")
    conn.commit()
    p = pathlib.Path(adif_path)
    if not p.exists():
        return 0
    with open(p, "r", encoding="utf-8", errors="replace") as f:
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

def rewrite_adif(adif_path, conn: sqlite3.Connection, mycall=""):
    rows = conn.execute(
        "SELECT * FROM qso ORDER BY date ASC, time_on ASC").fetchall()
    with open(adif_path, "w", encoding="utf-8") as f:
        f.write(adif_header(mycall))
        for row in rows:
            f.write(row_to_adif(row, mycall))

# ── Frequency / band ──────────────────────────────────────────────────────────
def freq_to_band(freq_mhz) -> str:
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

_PHONE_MODES = {"SSB","USB","LSB","AM","FM","FMN","PHONE"}
_US_PHONE_BANDS = {
    "Extra":      [(1.800,2.000),(3.600,4.000),(5.3305,5.4035),(7.125,7.300),
                   (14.150,14.350),(18.110,18.168),(21.200,21.450),(24.930,24.990),
                   (28.000,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
    "General":    [(1.843,2.000),(3.800,4.000),(5.3305,5.4035),(7.175,7.300),
                   (14.225,14.350),(18.110,18.168),(21.275,21.450),(24.930,24.990),
                   (28.300,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
    "Technician": [(28.300,29.700),(50.0,54.0),(144.0,148.0),(420.0,450.0)],
}
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

def _in_band(freq_mhz: float, license_class: str, mode: str = "") -> bool:
    plan = _US_PHONE_BANDS if mode.strip().upper() in _PHONE_MODES else _US_CW_BANDS
    ranges = plan.get(license_class)
    if not ranges:
        return True
    return any(lo <= freq_mhz <= hi for lo, hi in ranges)

# ── Flrig XML-RPC ─────────────────────────────────────────────────────────────
class _TimeoutTransport(xmlrpc.client.Transport):
    def __init__(self, timeout=2.0):
        super().__init__()
        self._timeout = timeout
    def make_connection(self, host):
        conn = super().make_connection(host)
        conn.timeout = self._timeout
        return conn

def flrig_get_all(host: str, port: int):
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

def ssb_mode_for_freq(freq_hz: float) -> str:
    mhz = float(freq_hz) / 1_000_000
    if 5.3305 <= mhz <= 5.4035:
        return "USB"
    return "USB" if mhz >= 10.0 else "LSB"

def flrig_set_freq_and_mode(host: str, port: int, freq_hz: float, spot_mode: str = ""):
    try:
        proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}/RPC2",
            transport=_TimeoutTransport(timeout=5.0),
            allow_none=True)
        proxy.rig.set_vfo(float(freq_hz))
        norm = spot_mode.strip().upper()
        if norm in ("SSB", "USB", "LSB", ""):
            proxy.rig.set_mode(ssb_mode_for_freq(freq_hz))
        elif norm:
            proxy.rig.set_mode(norm)
        return True
    except Exception as e:
        return str(e)

def flrig_set_ptt(host: str, port: int, state: bool) -> bool:
    try:
        proxy = xmlrpc.client.ServerProxy(
            f"http://{host}:{port}/RPC2",
            transport=_TimeoutTransport(timeout=2.0),
            allow_none=True)
        proxy.rig.set_ptt(1 if state else 0)
        return True
    except Exception:
        return False

# ── POTA spots ────────────────────────────────────────────────────────────────

def _itu_region_from_lon(lon: float) -> int:
    if lon <= -30:
        return 2
    if lon <= 60:
        return 1
    return 3

def fetch_pota_spots() -> list:
    url = "https://api.pota.app/spot/activator"
    try:
        req = urllib.request.Request(
            url, headers={"User-Agent": "POTA-Hunter-TUI/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            data = json.loads(r.read())
        if not isinstance(data, list):
            return []
        return data
    except Exception:
        return []

def filter_spots(spots: list, cfg: dict) -> list:
    band_f   = cfg.get("pota_band", "All")
    mode_f   = cfg.get("pota_mode", "All")
    hide_qrt = cfg.get("pota_hide_qrt", False)
    hide_oob = cfg.get("pota_hide_oob", False)
    r1       = cfg.get("pota_itu_r1", True)
    r2       = cfg.get("pota_itu_r2", True)
    r3       = cfg.get("pota_itu_r3", True)
    lic      = cfg.get("license_class", "Extra")
    now      = datetime.datetime.now(datetime.timezone.utc)
    out = []
    for s in spots:
        freq_str = str(s.get("frequency", "") or "")
        try:
            freq_khz = float(freq_str)
            freq_mhz = freq_khz / 1000.0
        except ValueError:
            freq_mhz = 0.0
            freq_khz = 0.0
        mode = str(s.get("mode", "") or "").upper()
        band = freq_to_band(freq_mhz) if freq_mhz else ""
        if band_f != "All" and band != band_f:
            continue
        if mode_f != "All" and mode != mode_f.upper():
            continue
        if hide_oob and freq_mhz and not _in_band(freq_mhz, lic, mode):
            continue
        if hide_qrt:
            spot_time_str = s.get("spotTime", "") or ""
            try:
                st = datetime.datetime.fromisoformat(
                    spot_time_str.replace("Z", "+00:00"))
                if (now - st).total_seconds() > 3600:
                    continue
            except Exception:
                pass
        if not (r1 and r2 and r3):
            try:
                lon = float(s.get("longitude") or s.get("lon") or 0)
            except (ValueError, TypeError):
                lon = 0.0
            itu = _itu_region_from_lon(lon)
            if itu == 1 and not r1:
                continue
            if itu == 2 and not r2:
                continue
            if itu == 3 and not r3:
                continue
        out.append(s)
    return out

def pota_post_spot(activator, spotter, reference, freq_khz, mode, comment=""):
    body = json.dumps({
        "activator":  activator,
        "spotter":    spotter,
        "reference":  reference,
        "frequency":  str(freq_khz),
        "mode":       mode,
        "comments":   comment or "Spotted via POTA Hunter TUI",
    }).encode()
    req = urllib.request.Request(
        "https://api.pota.app/spot",
        data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "User-Agent":   "POTA-Hunter-TUI/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=10):
            return True, None
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except Exception as e:
        return False, str(e)

# ── Parks DB ──────────────────────────────────────────────────────────────────
def _latlon_to_grid(lat, lon) -> str:
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return ""
    lon += 180.0; lat += 90.0
    return (chr(ord('A') + int(lon / 20)) +
            chr(ord('A') + int(lat / 10)) +
            str(int((lon % 20) / 2)) +
            str(int(lat % 10)))

def parks_db_exists() -> bool:
    if not PARKS_DB.exists():
        return False
    try:
        with sqlite3.connect(PARKS_DB) as cx:
            return cx.execute("SELECT COUNT(*) FROM parks").fetchone()[0] > 0
    except Exception:
        return False

def lookup_park(reference: str) -> Optional[dict]:
    if not PARKS_DB.exists():
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

def build_parks_db(progress_cb=None):
    if progress_cb:
        progress_cb("Downloading POTA parks list…")
    try:
        req = urllib.request.Request(
            PARKS_CSV_URL, headers={"User-Agent": "POTA-Hunter-TUI/1.0"})
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        return 0, f"Download failed: {e}"
    if progress_cb:
        progress_cb("Parsing CSV…")
    FIELD_MAP = {
        "reference": ["reference", "park_reference"],
        "name":      ["name", "park_name"],
        "latitude":  ["latitude", "lat"],
        "longitude": ["longitude", "lon", "long"],
        "grid":      ["grid", "gridsquare"],
        "state":     ["locationname", "state"],
        "country":   ["entityname", "country"],
    }
    rows = []
    for rec in csv.DictReader(io.StringIO(raw)):
        lc = {k.strip().lower(): v.strip() for k, v in rec.items()}
        def pick(candidates):
            for c in candidates:
                if lc.get(c): return lc[c]
            return ""
        ref = pick(FIELD_MAP["reference"]).upper()
        if not ref: continue
        lat  = pick(FIELD_MAP["latitude"])
        lon  = pick(FIELD_MAP["longitude"])
        grid = pick(FIELD_MAP["grid"]) or _latlon_to_grid(lat, lon)
        rows.append((ref, pick(FIELD_MAP["name"]), lat, lon,
                     grid.upper()[:6], pick(FIELD_MAP["state"]), pick(FIELD_MAP["country"])))
    if not rows:
        return 0, "No usable park records in CSV."
    if progress_cb:
        progress_cb(f"Writing {len(rows):,} parks…")
    try:
        with sqlite3.connect(PARKS_DB) as cx:
            cx.execute("DROP TABLE IF EXISTS parks")
            cx.execute("""CREATE TABLE parks (
                reference TEXT PRIMARY KEY, name TEXT,
                latitude REAL, longitude REAL, grid TEXT,
                state TEXT, country TEXT)""")
            cx.executemany("INSERT OR REPLACE INTO parks VALUES (?,?,?,?,?,?,?)", rows)
    except Exception as e:
        return 0, f"DB write failed: {e}"
    if progress_cb:
        progress_cb(f"Parks DB ready — {len(rows):,} parks.")
    return len(rows), None

# ── FCC DB ────────────────────────────────────────────────────────────────────
def fcc_db_exists() -> bool:
    if not FCC_DB.exists():
        return False
    try:
        with sqlite3.connect(FCC_DB) as cx:
            return cx.execute("SELECT COUNT(*) FROM callsigns").fetchone()[0] > 0
    except Exception:
        return False

def fcc_lookup(call: str) -> Optional[dict]:
    if not FCC_DB.exists():
        return None
    try:
        with sqlite3.connect(FCC_DB) as cx:
            row = cx.execute(
                "SELECT first_name,last_name,addr1,city,state,lic_class "
                "FROM callsigns WHERE call=?", (call.upper(),)).fetchone()
    except Exception:
        return None
    if not row:
        return None
    fn, ln, addr, city, state, cls = row
    return {"name": f"{fn} {ln}".strip(),
            "qth":  f"{city}, {state}".strip(", "),
            "class": cls, "source": "FCC"}

def build_fcc_db(progress_cb=None):
    if progress_cb:
        progress_cb("Connecting to FCC server…")
    raw = None; last_err = ""
    for url in FCC_ZIP_URLS:
        source = "FCC" if "fcc.gov" in url else "mirror"
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "POTA-Hunter-TUI/1.0"})
            with urllib.request.urlopen(req, timeout=30) as resp:
                total = int(resp.headers.get("Content-Length") or 0)
                chunks = []; received = 0
                while True:
                    chunk = resp.read(65536)
                    if not chunk: break
                    chunks.append(chunk); received += len(chunk)
                    if progress_cb:
                        mb = received / 1_048_576
                        if total:
                            progress_cb(f"Downloading FCC ({source})… {mb:.1f}/{total/1_048_576:.1f} MB ({received*100//total}%)")
                        else:
                            progress_cb(f"Downloading FCC ({source})… {mb:.1f} MB")
            raw = b"".join(chunks); break
        except Exception as e:
            last_err = str(e)
            if progress_cb: progress_cb(f"{source} failed ({e}) — trying next…")
    if raw is None:
        return 0, f"Download failed: {last_err}"
    if progress_cb: progress_cb("Parsing FCC data…")
    try:
        zf = zipfile.ZipFile(io.BytesIO(raw))
        active = set()
        with zf.open("HD.dat") as f:
            for line in f:
                parts = line.decode("latin-1").rstrip("\r\n").split("|")
                if len(parts) > 5 and parts[5] == "A":
                    active.add(parts[4].upper())
        classes = {}
        with zf.open("AM.dat") as f:
            for line in f:
                parts = line.decode("latin-1").rstrip("\r\n").split("|")
                if len(parts) > 5 and parts[4]:
                    classes[parts[4].upper()] = parts[5]
        rows = []
        with zf.open("EN.dat") as f:
            for line in f:
                parts = line.decode("latin-1").rstrip("\r\n").split("|")
                if len(parts) < 19: continue
                call = parts[4].upper()
                if not call or call not in active: continue
                first = parts[8].strip(); last = parts[10].strip()
                entity = parts[7].strip()
                if not first and not last and entity: first = entity
                rows.append((call, first, last, parts[15].strip(),
                             parts[16].strip(), parts[17].strip(),
                             parts[18].strip(), classes.get(call, "")))
    except Exception as e:
        return 0, f"Parse failed: {e}"
    if not rows:
        return 0, "No active licensee records found."
    if progress_cb: progress_cb(f"Writing {len(rows):,} callsigns…")
    try:
        with sqlite3.connect(FCC_DB) as cx:
            cx.execute("DROP TABLE IF EXISTS callsigns")
            cx.execute("""CREATE TABLE callsigns (
                call TEXT PRIMARY KEY, first_name TEXT, last_name TEXT,
                addr1 TEXT, city TEXT, state TEXT, zip TEXT, lic_class TEXT)""")
            cx.execute("CREATE INDEX IF NOT EXISTS idx_fcc ON callsigns(call)")
            cx.executemany("INSERT OR REPLACE INTO callsigns VALUES (?,?,?,?,?,?,?,?)", rows)
    except Exception as e:
        return 0, f"DB write failed: {e}"
    if progress_cb: progress_cb(f"FCC DB ready — {len(rows):,} callsigns.")
    return len(rows), None

# ── QRZ / HamDB ───────────────────────────────────────────────────────────────
_qrz_session: Optional[str] = None

def qrz_login(user: str, password: str):
    global _qrz_session
    url = (f"https://xmldata.qrz.com/xml/current/?username={urllib.parse.quote(user)}"
           f"&password={urllib.parse.quote(password)}&agent=POTA-Hunter-TUI/1.0")
    try:
        with urllib.request.urlopen(url, timeout=8) as r:
            tree = ET.parse(r)
        key = tree.find(".//{http://xmldata.qrz.com}Key")
        if key is not None:
            _qrz_session = key.text; return True
        err = tree.find(".//{http://xmldata.qrz.com}Error")
        return err.text if err is not None else "Login failed"
    except Exception as e:
        return str(e)

def qrz_lookup(call: str) -> Optional[dict]:
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
                "grid": g("grid"), "class": g("class"), "source": "QRZ"}
    except Exception:
        return None

def hamdb_lookup(call: str) -> Optional[dict]:
    url = f"http://api.hamdb.org/{urllib.parse.quote(call.upper())}/json/POTA-Hunter-TUI"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "POTA-Hunter-TUI/1.0"})
        with urllib.request.urlopen(req, timeout=8) as r:
            data = json.loads(r.read())["hamdb"]["callsign"]
        if data.get("call", "").upper() == "NOT_FOUND":
            return None
        city  = data.get("addr2", ""); state = data.get("state", "")
        return {"name":  f"{data.get('fname','')} {data.get('name','')}".strip(),
                "qth":   f"{city}, {state}".strip(", "),
                "grid":  data.get("grid", ""),
                "class": data.get("class", ""),
                "source":"HamDB"}
    except Exception:
        return None

# ── Section 3: Shared state dataclasses ──────────────────────────────────────

@dataclasses.dataclass
class FlrigState:
    freq:      float = 0.0
    mode:      str   = ""
    smeter:    float = 0.0
    pwr:       float = 0.0
    ptt:       bool  = False
    connected: bool  = False
    _lock: object = dataclasses.field(default_factory=threading.Lock, repr=False)

    def update(self, freq, mode, smeter, pwr, ptt, connected):
        with self._lock:
            if freq is not None:
                try: self.freq = float(freq) / 1_000_000
                except Exception: pass
            if mode is not None: self.mode = str(mode)
            if smeter is not None:
                try: self.smeter = float(smeter)
                except Exception: pass
            if pwr is not None:
                try: self.pwr = float(pwr)
                except Exception: pass
            self.ptt = ptt
            self.connected = connected

    def snapshot(self):
        with self._lock:
            return dataclasses.replace(self)

class SpotState:
    def __init__(self):
        self._lock    = threading.RLock()
        self._all     = []
        self._filtered = []
        self.timestamp = None

    def update(self, all_spots: list, filtered: list):
        with self._lock:
            self._all      = all_spots
            self._filtered = filtered
            self.timestamp = datetime.datetime.now(datetime.timezone.utc)

    def get_filtered(self) -> list:
        with self._lock:
            return list(self._filtered)

    def get_all(self) -> list:
        with self._lock:
            return list(self._all)

@dataclasses.dataclass
class LookupResult:
    callsign:      str = ""
    name:          str = ""
    qth:           str = ""
    license_class: str = ""
    grid:          str = ""
    source:        str = ""

@dataclasses.dataclass
class ParkResult:
    ref:     str = ""
    name:    str = ""
    state:   str = ""
    country: str = ""
    grid:    str = ""

@dataclasses.dataclass
class StatusMessage:
    text:  str = ""
    level: str = "info"   # info / warn / error / ok

# Event types posted from workers to event_queue
@dataclasses.dataclass
class EvFlrigUpdate:
    freq: Optional[float]; mode: Optional[str]
    smeter: Optional[float]; pwr: Optional[float]
    ptt: bool; connected: bool

@dataclasses.dataclass
class EvSpotUpdate:
    all_spots: list; filtered: list

@dataclasses.dataclass
class EvLookupResult:
    result: LookupResult

@dataclasses.dataclass
class EvParkResult:
    result: ParkResult

@dataclasses.dataclass
class EvStatus:
    msg: StatusMessage

@dataclasses.dataclass
class EvTuneRequest:
    freq_hz: float; mode: str

# ── Section 4: Background worker threads ──────────────────────────────────────

class FlrigPoller(threading.Thread):
    def __init__(self, event_queue: queue.Queue, cfg: dict):
        super().__init__(daemon=True, name="flrig-poller")
        self._q    = event_queue
        self._cfg  = cfg
        self._stop = threading.Event()

    def stop(self): self._stop.set()

    def run(self):
        host = self._cfg.get("flrig_host", "127.0.0.1")
        port = int(self._cfg.get("flrig_port", 12345))
        interval = float(self._cfg.get("flrig_poll_interval", 1.5))
        while not self._stop.is_set():
            freq_hz, mode, sm, pwr, ptt = flrig_get_all(host, port)
            connected = freq_hz is not None
            try:
                self._q.put_nowait(EvFlrigUpdate(
                    freq=freq_hz, mode=mode, smeter=sm,
                    pwr=pwr, ptt=ptt, connected=connected))
            except queue.Full:
                pass
            self._stop.wait(interval)


class SpotRefresher(threading.Thread):
    def __init__(self, event_queue: queue.Queue, cfg: dict, spot_state: SpotState):
        super().__init__(daemon=True, name="spot-refresher")
        self._q          = event_queue
        self._cfg        = cfg
        self._spot_state = spot_state
        self._stop       = threading.Event()
        self._trigger    = threading.Event()

    def stop(self): self._stop.set()

    def trigger_now(self):
        self._trigger.set()

    def run(self):
        interval = float(self._cfg.get("spot_refresh_interval", 120))
        while not self._stop.is_set():
            self._do_refresh()
            self._trigger.clear()
            # wait for interval or manual trigger
            self._trigger.wait(timeout=interval)

    def _do_refresh(self):
        try:
            self._q.put_nowait(EvStatus(StatusMessage("Fetching POTA spots…", "info")))
        except queue.Full:
            pass
        spots = fetch_pota_spots()
        filtered = filter_spots(spots, self._cfg)
        self._spot_state.update(spots, filtered)
        try:
            self._q.put_nowait(EvSpotUpdate(all_spots=spots, filtered=filtered))
        except queue.Full:
            pass


class LookupWorker(threading.Thread):
    def __init__(self, event_queue: queue.Queue, cfg: dict):
        super().__init__(daemon=True, name="lookup-worker")
        self._q     = event_queue
        self._cfg   = cfg
        self._inbox = queue.Queue(maxsize=5)
        self._stop  = threading.Event()

    def stop(self): self._stop.set()

    def trigger(self, callsign: str):
        try:
            self._inbox.put_nowait(callsign.strip().upper())
        except queue.Full:
            pass

    def run(self):
        while not self._stop.is_set():
            try:
                call = self._inbox.get(timeout=0.5)
            except queue.Empty:
                continue
            self._lookup(call)

    def _lookup(self, call: str):
        result = LookupResult(callsign=call)
        park_result = None

        # Callsign lookup: FCC → QRZ → HamDB
        info = fcc_lookup(call)
        if info:
            result.name          = info.get("name", "")
            result.qth           = info.get("qth", "")
            result.license_class = info.get("class", "")
            result.source        = "FCC"
        else:
            info = qrz_lookup(call) or hamdb_lookup(call)
            if info:
                result.name          = info.get("name", "")
                result.qth           = info.get("qth", "")
                result.license_class = info.get("class", "")
                result.grid          = info.get("grid", "")
                result.source        = info.get("source", "")

        try:
            self._q.put_nowait(EvLookupResult(result=result))
        except queue.Full:
            pass


class AutoScanner(threading.Thread):
    def __init__(self, event_queue: queue.Queue, spot_state: SpotState, cfg: dict):
        super().__init__(daemon=True, name="auto-scanner")
        self._q          = event_queue
        self._spot_state = spot_state
        self._cfg        = cfg
        self._stop       = threading.Event()
        self._enabled    = threading.Event()
        self._pos        = 0

    def stop(self):    self._stop.set()
    def enable(self):  self._enabled.set()
    def disable(self): self._enabled.clear()
    def is_enabled(self) -> bool: return self._enabled.is_set()
    def set_position(self, idx: int): self._pos = idx

    def run(self):
        while not self._stop.is_set():
            if not self._enabled.wait(timeout=1.0):
                continue
            interval = float(self._cfg.get("pota_scan_interval", 30))
            self._stop.wait(interval)
            if not self._enabled.is_set():
                continue
            spots = self._spot_state.get_filtered()
            if not spots:
                continue
            self._pos = (self._pos + 1) % len(spots)
            spot = spots[self._pos]
            try:
                freq_khz = float(spot.get("frequency", 0) or 0)
                if freq_khz > 0:
                    self._q.put_nowait(EvTuneRequest(
                        freq_hz=freq_khz * 1000,
                        mode=str(spot.get("mode", "") or "")))
            except (queue.Full, Exception):
                pass

# ── Section 5: Colors & drawing helpers ───────────────────────────────────────

# Color pair indices
CP_NORMAL   = 1
CP_TITLE    = 2
CP_SELECTED = 3
CP_WORKED   = 4
CP_OOB      = 5
CP_TX       = 6
CP_SMETER   = 7
CP_POWER    = 8
CP_LABEL    = 9
CP_FIELD    = 10
CP_BORDER   = 11
CP_ERROR    = 12
CP_SUCCESS  = 13
CP_QRT      = 14
CP_HEADER   = 15
CP_ACCENT   = 16

def setup_colors():
    curses.use_default_colors()
    bg = -1  # transparent background
    curses.init_pair(CP_NORMAL,   curses.COLOR_WHITE,   bg)
    curses.init_pair(CP_TITLE,    curses.COLOR_CYAN,    bg)
    curses.init_pair(CP_SELECTED, curses.COLOR_WHITE,   curses.COLOR_BLUE)
    curses.init_pair(CP_WORKED,   curses.COLOR_GREEN,   bg)
    curses.init_pair(CP_OOB,      curses.COLOR_RED,     bg)
    curses.init_pair(CP_TX,       curses.COLOR_RED,     bg)
    curses.init_pair(CP_SMETER,   curses.COLOR_GREEN,   bg)
    curses.init_pair(CP_POWER,    curses.COLOR_YELLOW,  bg)
    curses.init_pair(CP_LABEL,    curses.COLOR_CYAN,    bg)
    curses.init_pair(CP_FIELD,    curses.COLOR_WHITE,   curses.COLOR_BLUE)
    curses.init_pair(CP_BORDER,   curses.COLOR_BLUE,    bg)
    curses.init_pair(CP_ERROR,    curses.COLOR_RED,     bg)
    curses.init_pair(CP_SUCCESS,  curses.COLOR_GREEN,   bg)
    curses.init_pair(CP_QRT,      curses.COLOR_BLACK,   bg)  # dim
    curses.init_pair(CP_HEADER,   curses.COLOR_YELLOW,  bg)
    curses.init_pair(CP_ACCENT,   curses.COLOR_MAGENTA, bg)

def safe_addstr(win, y, x, s, attr=0):
    try:
        win.addstr(y, x, s, attr)
    except curses.error:
        pass

def safe_addch(win, y, x, ch, attr=0):
    try:
        win.addch(y, x, ch, attr)
    except curses.error:
        pass

def hline(win, y, x, ch, n, attr=0):
    for i in range(n):
        safe_addch(win, y, x + i, ch, attr)

def trunc(s: str, n: int) -> str:
    if n <= 0: return ""
    if len(s) <= n: return s
    return s[:n - 1] + ELLIPSIS

def age_str(spot_time_str: str) -> str:
    if not spot_time_str:
        return "?"
    try:
        st = datetime.datetime.fromisoformat(
            spot_time_str.replace("Z", "+00:00"))
        delta = (datetime.datetime.now(datetime.timezone.utc) - st).total_seconds()
        if delta < 0:
            return "0m"
        if delta > 3600:
            return "QRT"
        if delta >= 60:
            return f"{int(delta//60)}m"
        return f"{int(delta)}s"
    except Exception:
        return "?"

def draw_bar(win, y, x, width, value, max_val, filled_cp, empty_cp):
    if width <= 0:
        return
    pct    = max(0.0, min(1.0, float(value) / float(max_val))) if max_val else 0.0
    filled = int(pct * width)
    for i in range(width):
        ch   = BLOCK_FULL if i < filled else BLOCK_LIGHT
        attr = curses.color_pair(filled_cp if i < filled else empty_cp)
        safe_addch(win, y, x + i, ch, attr)

def draw_smeter(win, y, x, width, smeter_val):
    if width < 6:
        return
    s_label = f"S{int(smeter_val):X}" if smeter_val <= 9 else f"+{int(smeter_val-9)*6:02d}"
    label   = f"[{s_label} "
    suffix  = "]"
    bar_w   = width - len(label) - len(suffix)
    safe_addstr(win, y, x, label, curses.color_pair(CP_SMETER))
    draw_bar(win, y, x + len(label), bar_w, smeter_val, 9.0, CP_SMETER, CP_QRT)
    safe_addstr(win, y, x + len(label) + bar_w, suffix, curses.color_pair(CP_SMETER))

def draw_power_bar(win, y, x, width, pwr_pct):
    if width < 6:
        return
    label  = "["
    suffix = "]"
    bar_w  = width - 2
    safe_addstr(win, y, x, label, curses.color_pair(CP_POWER))
    draw_bar(win, y, x + 1, bar_w, pwr_pct, 100.0, CP_TX, CP_QRT)
    safe_addstr(win, y, x + 1 + bar_w, suffix, curses.color_pair(CP_POWER))

def draw_border(win, title="", title_attr=0):
    H, W = win.getmaxyx()
    battr = curses.color_pair(CP_BORDER)
    safe_addstr(win, 0, 0, B_TL, battr)
    hline(win, 0, 1, B_H, W - 2, battr)
    safe_addstr(win, 0, W - 1, B_TR, battr)
    safe_addstr(win, H - 1, 0, B_BL, battr)
    hline(win, H - 1, 1, B_H, W - 2, battr)
    safe_addstr(win, H - 1, W - 1, B_BR, battr)
    for row in range(1, H - 1):
        safe_addstr(win, row, 0, B_V, battr)
        safe_addstr(win, row, W - 1, B_V, battr)
    if title:
        t = f" {title} "
        col = max(2, (W - len(t)) // 2)
        safe_addstr(win, 0, col, t, title_attr or (curses.color_pair(CP_TITLE) | curses.A_BOLD))

# ── Section 6: Widget classes ─────────────────────────────────────────────────

class TextField:
    """Single-line in-place text editor."""

    def __init__(self, maxlen: int = 40, uppercase: bool = False):
        self.maxlen    = maxlen
        self.uppercase = uppercase
        self._buf: List[str] = []
        self._cur  = 0

    def value(self) -> str:
        return "".join(self._buf)

    def set_value(self, s: str):
        s = s[:self.maxlen]
        if self.uppercase:
            s = s.upper()
        self._buf = list(s)
        self._cur = len(self._buf)

    def clear(self):
        self._buf = []; self._cur = 0

    def handle_key(self, ch: int) -> bool:
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            if self._cur > 0:
                del self._buf[self._cur - 1]
                self._cur -= 1
            return True
        if ch == curses.KEY_DC:
            if self._cur < len(self._buf):
                del self._buf[self._cur]
            return True
        if ch == curses.KEY_LEFT:
            self._cur = max(0, self._cur - 1); return True
        if ch == curses.KEY_RIGHT:
            self._cur = min(len(self._buf), self._cur + 1); return True
        if ch == curses.KEY_HOME:
            self._cur = 0; return True
        if ch == curses.KEY_END:
            self._cur = len(self._buf); return True
        if ch == 21:  # Ctrl-U
            self.clear(); return True
        if 32 <= ch <= 126:
            c = chr(ch)
            if self.uppercase:
                c = c.upper()
            if len(self._buf) < self.maxlen:
                self._buf.insert(self._cur, c)
                self._cur += 1
            return True
        return False

    def draw(self, win, y: int, x: int, width: int, focused: bool):
        val = self.value()
        # Scroll view so cursor is visible
        if self._cur >= width:
            view_start = self._cur - width + 1
        else:
            view_start = 0
        display = val[view_start:view_start + width]
        display = display.ljust(width)
        attr = curses.color_pair(CP_FIELD) if focused else curses.color_pair(CP_NORMAL)
        safe_addstr(win, y, x, display[:width], attr)
        if focused:
            cur_x = self._cur - view_start
            if 0 <= cur_x < width:
                ch = display[cur_x] if cur_x < len(display) else " "
                safe_addstr(win, y, x + cur_x, ch,
                            attr | curses.A_REVERSE)


class StatusBar:
    def __init__(self, win):
        self.win = win

    def draw(self, flrig: FlrigState, cfg: dict,
             logbook_path: Optional[pathlib.Path],
             spot_count: int, status_msg: StatusMessage,
             activator_mode: bool = False):
        win = self.win
        H, W = win.getmaxyx()
        win.erase()
        ta = curses.color_pair(CP_TITLE) | curses.A_BOLD

        # Row 0: title bar
        mycall  = cfg.get("callsign", "") or "?"
        lb_name = logbook_path.stem if logbook_path else "(no logbook)"
        title   = f" POTA Hunter TUI  OP:{mycall}  Log:{lb_name}"
        safe_addstr(win, 0, 0, B_H * W, curses.color_pair(CP_BORDER))
        safe_addstr(win, 0, 1, trunc(title, W - 2), ta)
        if activator_mode and int(time.monotonic()) % 2 == 0:
            act_label = f" [ACTIVATED: {cfg.get('my_park','')}] "
            act_col   = max(len(title) + 2, W - len(act_label) - 2)
            safe_addstr(win, 0, act_col,
                        trunc(act_label, W - act_col - 1),
                        curses.color_pair(CP_TX) | curses.A_BOLD)

        # Row 1: VFO / telemetry
        if H >= 2:
            fs = flrig
            if fs.connected:
                freq_str = f"{fs.freq:.3f} MHz" if fs.freq else "---"
                mode_str = fs.mode or "---"
                ptt_str  = " [TX]" if fs.ptt else "     "
                ptt_attr = curses.color_pair(CP_TX) | curses.A_BOLD if fs.ptt else curses.color_pair(CP_NORMAL)
                parts = f" VFO:{freq_str} {mode_str}  "
                safe_addstr(win, 1, 0, parts, curses.color_pair(CP_NORMAL))
                col = len(parts)
                # S-meter
                sm_w = min(18, W - col - 20)
                if sm_w > 4:
                    draw_smeter(win, 1, col, sm_w, fs.smeter)
                    col += sm_w + 1
                # Power
                pw_w = min(14, W - col - 12)
                if pw_w > 4:
                    safe_addstr(win, 1, col, " Pwr:", curses.color_pair(CP_POWER))
                    col += 5
                    draw_power_bar(win, 1, col, pw_w, fs.pwr)
                    col += pw_w + 1
                safe_addstr(win, 1, col, ptt_str, ptt_attr)
                col += len(ptt_str)
            else:
                safe_addstr(win, 1, 0, " Flrig: not connected", curses.color_pair(CP_ERROR))
                col = 22

            # Spots count + status
            spots_str = f"  {spot_count} spots"
            safe_addstr(win, 1, max(col, W - len(spots_str) - 1),
                        spots_str, curses.color_pair(CP_ACCENT))

            # Status message overlay
            if status_msg and status_msg.text:
                attr = {
                    "info":  curses.color_pair(CP_NORMAL),
                    "ok":    curses.color_pair(CP_SUCCESS),
                    "warn":  curses.color_pair(CP_POWER),
                    "error": curses.color_pair(CP_ERROR),
                }.get(status_msg.level, curses.color_pair(CP_NORMAL))
                msg = trunc(f"  {status_msg.text}", W // 2)
                safe_addstr(win, 1, W // 2, msg, attr)

        win.noutrefresh()


class SpotListPanel:
    COLS = [
        ("Activator", 10), ("Park", 8), ("Freq", 9),
        ("Mode", 5), ("Park Name", 14), ("Comment", 12), ("Age", 4),
    ]

    def __init__(self, win):
        self.win        = win
        self.spots:     List[dict] = []
        self.sel_idx    = 0
        self.offset     = 0
        self._worked:   set = set()
        self._park_names: Dict[str, str] = {}

    def _fetch_park_names(self, spots: list) -> Dict[str, str]:
        if not spots or not PARKS_DB.exists():
            return {}
        refs = list({str(s.get("reference", "")) for s in spots if s.get("reference")})
        if not refs:
            return {}
        try:
            placeholders = ",".join("?" * len(refs))
            with sqlite3.connect(PARKS_DB) as cx:
                rows = cx.execute(
                    f"SELECT reference, name FROM parks WHERE reference IN ({placeholders})",
                    refs).fetchall()
            return {r[0]: r[1] for r in rows}
        except Exception:
            return {}

    def set_spots(self, spots: list, worked_calls: set):
        self.spots        = spots
        self._worked      = worked_calls
        self._park_names  = self._fetch_park_names(spots)
        self.sel_idx = min(self.sel_idx, max(0, len(spots) - 1))

    def selected_spot(self) -> Optional[dict]:
        if self.spots and 0 <= self.sel_idx < len(self.spots):
            return self.spots[self.sel_idx]
        return None

    def _col_widths(self, W: int) -> List[int]:
        inner = W - 2
        # activator(10)+park(8)+freq(9)+mode(5)+age(4) + 6 separators + 2 prefix
        fixed = 10 + 8 + 9 + 5 + 4 + 6 + 2
        remaining = max(14, inner - fixed)
        name_w    = max(8,  remaining // 3)
        comment_w = max(6,  remaining - name_w)
        return [10, 8, 9, 5, name_w, comment_w, 4]

    def draw(self, cfg: dict):
        win  = self.win
        H, W = win.getmaxyx()
        win.erase()
        battr = curses.color_pair(CP_BORDER)

        # Borders
        safe_addstr(win, 0, 0, B_TL, battr)
        hline(win, 0, 1, B_H, W - 2, battr)
        safe_addstr(win, 0, W - 1, B_TR, battr)
        for r in range(1, H - 1):
            safe_addstr(win, r, 0, B_V, battr)
            safe_addstr(win, r, W - 1, B_V, battr)
        safe_addstr(win, H - 1, 0, B_BL, battr)
        hline(win, H - 1, 1, B_H, W - 2, battr)
        safe_addstr(win, H - 1, W - 1, B_BR, battr)

        # Title
        n_all = len(self.spots)
        title = f" POTA Spots ({n_all}) "
        safe_addstr(win, 0, 2, title, curses.color_pair(CP_TITLE) | curses.A_BOLD)

        # Header row
        widths = self._col_widths(W)
        hattr  = curses.color_pair(CP_HEADER) | curses.A_BOLD
        col = 1
        for (name, _), w in zip(self.COLS, widths):
            safe_addstr(win, 1, col, name[:w].ljust(w), hattr)
            col += w + 1

        # Spot rows
        visible = H - 3  # rows 2..(H-2)
        if self.sel_idx < self.offset:
            self.offset = self.sel_idx
        if self.sel_idx >= self.offset + visible:
            self.offset = self.sel_idx - visible + 1

        now = datetime.datetime.now(datetime.timezone.utc)
        lic = cfg.get("license_class", "Extra")

        for i, spot in enumerate(self.spots[self.offset:self.offset + visible]):
            row = i + 2
            call = str(spot.get("activator", "") or "").upper()
            park = str(spot.get("reference", "") or "")
            freq_str = str(spot.get("frequency", "") or "")
            mode = str(spot.get("mode", "") or "")
            comment = str(spot.get("comments", "") or "")
            age  = age_str(str(spot.get("spotTime", "") or ""))

            try:
                fmhz = float(freq_str) / 1000.0 if freq_str else 0.0
            except ValueError:
                fmhz = 0.0

            is_sel    = (self.offset + i) == self.sel_idx
            is_worked = call in self._worked
            is_qrt    = age == "QRT"
            is_oob    = fmhz > 0 and not _in_band(fmhz, lic, mode.upper())

            if is_sel:
                attr = curses.color_pair(CP_SELECTED) | curses.A_BOLD
            elif is_worked:
                attr = curses.color_pair(CP_WORKED)
            elif is_qrt:
                attr = curses.color_pair(CP_QRT) | curses.A_DIM
            elif is_oob:
                attr = curses.color_pair(CP_OOB)
            else:
                attr = curses.color_pair(CP_NORMAL)

            prefix = ARROW + " " if is_sel else "  "
            safe_addstr(win, row, 1, prefix, attr)
            col = 3
            park_name = self._park_names.get(park, "")
            vals = [call, park, freq_str, mode, park_name, comment, age]
            for v, w in zip(vals, widths):
                safe_addstr(win, row, col, trunc(v, w).ljust(w), attr)
                col += w + 1

        win.noutrefresh()

    def handle_key(self, ch: int) -> bool:
        n = len(self.spots)
        if n == 0:
            return False
        if ch == curses.KEY_UP:
            self.sel_idx = max(0, self.sel_idx - 1); return True
        if ch == curses.KEY_DOWN:
            self.sel_idx = min(n - 1, self.sel_idx + 1); return True
        if ch == curses.KEY_PPAGE:
            self.sel_idx = max(0, self.sel_idx - 10); return True
        if ch == curses.KEY_NPAGE:
            self.sel_idx = min(n - 1, self.sel_idx + 10); return True
        if ch == curses.KEY_HOME:
            self.sel_idx = 0; return True
        if ch == curses.KEY_END:
            self.sel_idx = n - 1; return True
        return False


class QSOForm:
    FIELD_NAMES  = ["Callsign", "RST>", "RST<", "Park #", "Comment", "Notes"]
    FIELD_UPPER  = [True, False, False, True, False, False]
    FIELD_MAXLEN = [20, 6, 6, 15, 50, 80]
    DEFAULT_RST  = ["", "59", "59", "", "", ""]

    def __init__(self, win):
        self.win = win
        self.fields = [
            TextField(self.FIELD_MAXLEN[i], self.FIELD_UPPER[i])
            for i in range(len(self.FIELD_NAMES))
        ]
        self._reset_defaults()
        self.focus_idx   = 0
        self._last_key_t = 0.0
        self._lookup_pending = False

    def _reset_defaults(self):
        for i, fld in enumerate(self.fields):
            fld.set_value(self.DEFAULT_RST[i])

    def clear(self):
        for fld in self.fields:
            fld.clear()
        self._reset_defaults()
        self.focus_idx = 0

    def get_values(self) -> dict:
        fn = self.FIELD_NAMES
        vals = [f.value() for f in self.fields]
        return {
            "call":     vals[0],
            "rst_sent": vals[1] or "59",
            "rst_rcvd": vals[2] or "59",
            "park_nr":  vals[3],
            "comment":  vals[4],
            "notes":    vals[5],
        }

    def populate_from_spot(self, spot: dict):
        call = str(spot.get("activator", "") or "").upper()
        park = str(spot.get("reference", "") or "")
        self.fields[0].set_value(call)
        self.fields[3].set_value(park)
        self.focus_idx = 0

    def callsign_value(self) -> str:
        return self.fields[0].value().strip().upper()

    def park_value(self) -> str:
        return self.fields[3].value().strip().upper()

    def draw(self, lookup: Optional[LookupResult], park: Optional[ParkResult]):
        win = self.win
        H, W = win.getmaxyx()
        win.erase()
        battr = curses.color_pair(CP_BORDER)

        # Borders
        safe_addstr(win, 0, 0, B_TL, battr)
        hline(win, 0, 1, B_H, W - 2, battr)
        safe_addstr(win, 0, W - 1, B_TR, battr)
        for r in range(1, H - 1):
            safe_addstr(win, r, 0, B_V, battr)
            safe_addstr(win, r, W - 1, B_V, battr)
        safe_addstr(win, H - 1, 0, B_BL, battr)
        hline(win, H - 1, 1, B_H, W - 2, battr)
        safe_addstr(win, H - 1, W - 1, B_BR, battr)
        safe_addstr(win, 0, 2, " QSO Entry ", curses.color_pair(CP_TITLE) | curses.A_BOLD)

        inner = W - 2
        lattr = curses.color_pair(CP_LABEL) | curses.A_BOLD
        row = 1
        for i, (name, fld) in enumerate(zip(self.FIELD_NAMES, self.fields)):
            if row >= H - 1:
                break
            label   = f" {name}: "
            fld_x   = 1 + len(label)
            fld_w   = max(4, inner - len(label))
            safe_addstr(win, row, 1, label, lattr)
            fld.draw(win, row, fld_x, fld_w, focused=(i == self.focus_idx))
            row += 1

        # Buttons hint
        if row < H - 1:
            safe_addstr(win, row, 1,
                        " F2:Log  Esc:Clear  Tab:Next field",
                        curses.color_pair(CP_QRT))
            row += 1

        # Lookup divider + results
        if row < H - 1:
            safe_addstr(win, row, 1, B_H * (W - 2), battr)
            row += 1

        if lookup and lookup.name and row < H - 1:
            safe_addstr(win, row, 1,
                        trunc(f" {lookup.name} ({lookup.source})", inner),
                        curses.color_pair(CP_SUCCESS))
            row += 1
        if lookup and lookup.qth and row < H - 1:
            cls_str = f" [{lookup.license_class}]" if lookup.license_class else ""
            safe_addstr(win, row, 1,
                        trunc(f" {lookup.qth}{cls_str}", inner),
                        curses.color_pair(CP_NORMAL))
            row += 1
        if park and row < H - 1:
            safe_addstr(win, row, 1,
                        trunc(f" {park.ref}: {park.name}", inner),
                        curses.color_pair(CP_ACCENT))
            row += 1
        if park and (park.state or park.country) and row < H - 1:
            loc = ", ".join(filter(None, [park.state, park.country]))
            safe_addstr(win, row, 1,
                        trunc(f" {loc}  Grid:{park.grid}", inner),
                        curses.color_pair(CP_NORMAL))

        win.noutrefresh()

    def handle_key(self, ch: int) -> Optional[str]:
        """Returns 'log' if F2/Enter submitted, 'clear' if Esc, 'next-panel' if Tab from last field, else None."""
        if ch == curses.KEY_F2 or ch == ord('\n') or ch == ord('\r'):
            return "log"
        if ch == 27:  # Esc
            return "clear"
        if ch == ord('\t') or ch == curses.KEY_DOWN:
            self.focus_idx = (self.focus_idx + 1) % len(self.fields)
            return None
        if ch == curses.KEY_BTAB or ch == curses.KEY_UP:
            self.focus_idx = (self.focus_idx - 1) % len(self.fields)
            return None

        handled = self.fields[self.focus_idx].handle_key(ch)
        if handled and self.focus_idx == 0:
            self._last_key_t = time.monotonic()
            self._lookup_pending = True
        return None

    def should_trigger_lookup(self) -> bool:
        """Return True once if callsign field has ≥3 chars and 0.6s has elapsed."""
        if not self._lookup_pending:
            return False
        call = self.callsign_value()
        if len(call) < 3:
            return False
        if time.monotonic() - self._last_key_t >= 0.6:
            self._lookup_pending = False
            return True
        return False


class QSOLogPanel:
    COLS = [
        ("#",    3), ("Call",  9), ("Date",  8), ("Time", 4),
        ("Freq", 9), ("Band",  4), ("Mode",  5),
        ("S>",   3), ("S<",    3), ("Park", 10),
    ]

    def __init__(self, win):
        self.win     = win
        self.qsos:   list = []
        self.offset  = 0
        self.sel_idx = 0

    def set_qsos(self, qsos: list):
        self.qsos   = qsos
        self.sel_idx = min(self.sel_idx, max(0, len(qsos) - 1))

    def _col_widths(self, W: int) -> List[int]:
        inner   = W - 2
        fixed   = sum(w for _, w in self.COLS[:-1])
        park_w  = max(8, inner - fixed - len(self.COLS))
        return [3, 9, 8, 4, 9, 4, 5, 3, 3, park_w]

    def draw(self):
        win  = self.win
        H, W = win.getmaxyx()
        win.erase()
        battr = curses.color_pair(CP_BORDER)

        safe_addstr(win, 0, 0, B_LT, battr)
        hline(win, 0, 1, B_H, W - 2, battr)
        safe_addstr(win, 0, W - 1, B_RT, battr)
        for r in range(1, H - 1):
            safe_addstr(win, r, 0, B_V, battr)
            safe_addstr(win, r, W - 1, B_V, battr)
        safe_addstr(win, H - 1, 0, B_BL, battr)
        hline(win, H - 1, 1, B_H, W - 2, battr)
        safe_addstr(win, H - 1, W - 1, B_BR, battr)

        title = f" Recent QSOs ({len(self.qsos)}) "
        safe_addstr(win, 0, 2, title, curses.color_pair(CP_TITLE) | curses.A_BOLD)

        widths = self._col_widths(W)
        hattr  = curses.color_pair(CP_HEADER) | curses.A_BOLD
        col = 1
        for (name, _), w in zip(self.COLS, widths):
            safe_addstr(win, 1, col, name[:w].ljust(w), hattr)
            col += w + 1

        visible = H - 3
        n = len(self.qsos)
        if self.sel_idx < self.offset:
            self.offset = self.sel_idx
        if self.sel_idx >= self.offset + visible:
            self.offset = self.sel_idx - visible + 1

        for i, row_data in enumerate(self.qsos[self.offset:self.offset + visible]):
            r = dict(row_data)
            screen_row = i + 2
            is_sel = (self.offset + i) == self.sel_idx
            attr   = (curses.color_pair(CP_SELECTED) | curses.A_BOLD) if is_sel \
                     else curses.color_pair(CP_NORMAL)
            freq_str = f"{float(r.get('freq',0)):.3f}" if r.get('freq') else ""
            vals = [
                str(self.offset + i + 1),
                r.get("call", ""),
                r.get("date", "").replace("-", ""),
                r.get("time_on", ""),
                freq_str,
                r.get("band", ""),
                r.get("mode", ""),
                r.get("rst_sent", ""),
                r.get("rst_rcvd", ""),
                r.get("park_nr", ""),
            ]
            col = 1
            for v, w in zip(vals, widths):
                safe_addstr(win, screen_row, col, trunc(v, w).ljust(w), attr)
                col += w + 1

        win.noutrefresh()

    def handle_key(self, ch: int) -> bool:
        n = len(self.qsos)
        if ch == curses.KEY_UP:
            self.sel_idx = max(0, self.sel_idx - 1); return True
        if ch == curses.KEY_DOWN:
            self.sel_idx = min(max(0, n - 1), self.sel_idx + 1); return True
        if ch == curses.KEY_PPAGE:
            self.sel_idx = max(0, self.sel_idx - 5); return True
        if ch == curses.KEY_NPAGE:
            self.sel_idx = min(max(0, n - 1), self.sel_idx + 5); return True
        return False


class FilterBar:
    BANDS = ["All","160m","80m","60m","40m","30m","20m","17m","15m","12m","10m","6m","2m"]
    MODES = ["All","SSB","CW","FT8","FT4","RTTY","PSK31"]
    ITUS  = ["All", "1", "2", "3"]

    def __init__(self, win):
        self.win = win

    def draw(self, cfg: dict, scan_enabled: bool):
        win = self.win
        H, W = win.getmaxyx()
        win.erase()
        battr = curses.color_pair(CP_BORDER)
        safe_addstr(win, 0, 0, B_LT, battr)
        hline(win, 0, 1, B_H, W - 2, battr)
        safe_addstr(win, 0, W - 1, B_RT, battr)

        band  = cfg.get("pota_band", "All")
        mode  = cfg.get("pota_mode", "All")
        qrt   = "[x]" if cfg.get("pota_hide_qrt") else "[ ]"
        oob   = "[x]" if cfg.get("pota_hide_oob") else "[ ]"
        scan  = "ON " if scan_enabled else "OFF"
        itu   = "".join([
            "1" if cfg.get("pota_itu_r1") else "-",
            "2" if cfg.get("pota_itu_r2") else "-",
            "3" if cfg.get("pota_itu_r3") else "-",
        ])
        line = (f" b:Band[{band}] m:Mode[{mode}] "
                f"x:{qrt}QRT o:{oob}OOB r:ITU[{itu}] "
                f"F6:Scan[{scan}]")
        safe_addstr(win, 0, 0, B_LT, battr)
        safe_addstr(win, 0, 1, trunc(line, W - 2), curses.color_pair(CP_NORMAL))
        safe_addstr(win, 0, W - 1, B_RT, battr)
        win.noutrefresh()

    def handle_key(self, ch: int, cfg: dict) -> bool:
        if ch == ord('b'):
            cur = cfg.get("pota_band", "All")
            idx = (self.BANDS.index(cur) + 1) % len(self.BANDS) if cur in self.BANDS else 0
            cfg["pota_band"] = self.BANDS[idx]; return True
        if ch == ord('m'):
            cur = cfg.get("pota_mode", "All")
            idx = (self.MODES.index(cur) + 1) % len(self.MODES) if cur in self.MODES else 0
            cfg["pota_mode"] = self.MODES[idx]; return True
        if ch == ord('x'):
            cfg["pota_hide_qrt"] = not cfg.get("pota_hide_qrt", False); return True
        if ch == ord('o'):
            cfg["pota_hide_oob"] = not cfg.get("pota_hide_oob", False); return True
        if ch == ord('r'):
            r1 = cfg.get("pota_itu_r1", True)
            r2 = cfg.get("pota_itu_r2", True)
            r3 = cfg.get("pota_itu_r3", True)
            # Cycle: all → R1 only → R2 only → R3 only → all
            if r1 and r2 and r3:
                cfg["pota_itu_r1"]=True;  cfg["pota_itu_r2"]=False; cfg["pota_itu_r3"]=False
            elif r1:
                cfg["pota_itu_r1"]=False; cfg["pota_itu_r2"]=True;  cfg["pota_itu_r3"]=False
            elif r2:
                cfg["pota_itu_r1"]=False; cfg["pota_itu_r2"]=False; cfg["pota_itu_r3"]=True
            else:
                cfg["pota_itu_r1"]=True;  cfg["pota_itu_r2"]=True;  cfg["pota_itu_r3"]=True
            return True
        return False


class HelpBar:
    TEXT = ("F1:Focus  F2:Log  F3:Tune  F4:Respot  F5:Refresh  F6:Scan  "
            "F7:Logbook  F8:PostSpot  F9:Config  F10:DBTools  F12:ActMode  ?:Help  q:Quit")

    def __init__(self, win):
        self.win = win

    def draw(self):
        win = self.win
        H, W = win.getmaxyx()
        win.erase()
        safe_addstr(win, 0, 0, trunc(self.TEXT, W),
                    curses.color_pair(CP_QRT) | curses.A_BOLD)
        win.noutrefresh()


class ModalDialog:
    @staticmethod
    def message(stdscr, title: str, text: str):
        H, W = stdscr.getmaxyx()
        lines = text.split("\n")
        dh = min(len(lines) + 4, H - 4)
        dw = min(max(len(title) + 4, max((len(l) for l in lines), default=20) + 4), W - 4)
        y0 = (H - dh) // 2; x0 = (W - dw) // 2
        win = curses.newwin(dh, dw, y0, x0)
        draw_border(win, title)
        for i, line in enumerate(lines[:dh - 4]):
            safe_addstr(win, i + 1, 2, trunc(line, dw - 4))
        safe_addstr(win, dh - 2, 2, "Press any key…", curses.color_pair(CP_QRT))
        win.refresh()
        win.getch()

    @staticmethod
    def confirm(stdscr, title: str, question: str) -> bool:
        H, W = stdscr.getmaxyx()
        dw = min(max(len(title), len(question)) + 8, W - 4)
        dh = 6
        y0 = (H - dh) // 2; x0 = (W - dw) // 2
        win = curses.newwin(dh, dw, y0, x0)
        draw_border(win, title)
        safe_addstr(win, 1, 2, trunc(question, dw - 4))
        safe_addstr(win, 3, 2, "Y = Yes   N / Esc = No", curses.color_pair(CP_QRT))
        win.refresh()
        while True:
            ch = win.getch()
            if ch in (ord('y'), ord('Y')): return True
            if ch in (ord('n'), ord('N'), 27): return False

    @staticmethod
    def input_line(stdscr, title: str, prompt: str, default: str = "") -> Optional[str]:
        H, W = stdscr.getmaxyx()
        dw = min(max(len(title), len(prompt) + len(default) + 6, 50) + 4, W - 4)
        dh = 6
        y0 = (H - dh) // 2; x0 = (W - dw) // 2
        win = curses.newwin(dh, dw, y0, x0)
        draw_border(win, title)
        safe_addstr(win, 1, 2, trunc(prompt, dw - 4), curses.color_pair(CP_LABEL))
        field = TextField(maxlen=dw - 4)
        field.set_value(default)
        while True:
            field.draw(win, 3, 2, dw - 4, focused=True)
            safe_addstr(win, dh - 2, 2, "Enter=OK  Esc=Cancel", curses.color_pair(CP_QRT))
            win.refresh()
            ch = win.getch()
            if ch in (ord('\n'), ord('\r')):
                return field.value()
            if ch == 27:
                return None
            field.handle_key(ch)

    @staticmethod
    def config_editor(stdscr, cfg: dict) -> Optional[dict]:
        FIELDS = [
            ("callsign",              "My Callsign",        False),
            ("gridsquare",            "My Grid Square",     True),
            ("my_park",               "My Park (activation)",True),
            ("license_class",         "License Class",      True),
            ("flrig_host",            "Flrig Host",         False),
            ("flrig_port",            "Flrig Port",         False),
            ("qrz_user",              "QRZ Username",       False),
            ("qrz_pass",              "QRZ Password",       False),
            ("pota_scan_interval",    "Scan Interval (s)",  False),
            ("spot_refresh_interval", "Spot Refresh (s)",   False),
            ("flrig_poll_interval",   "Flrig Poll (s)",     False),
        ]
        H, W = stdscr.getmaxyx()
        dh = min(len(FIELDS) + 6, H - 2)
        dw = min(70, W - 4)
        y0 = (H - dh) // 2; x0 = (W - dw) // 2
        win = curses.newwin(dh, dw, y0, x0)
        draw_border(win, "Configuration")
        new_cfg = cfg.copy()
        fields = []
        for key, label, up in FIELDS:
            tf = TextField(maxlen=40, uppercase=up)
            tf.set_value(str(new_cfg.get(key, "")))
            fields.append((key, label, tf))

        focus = 0
        offset = 0
        visible = dh - 4

        while True:
            win.erase()
            draw_border(win, "Configuration  (Enter/s=Save  Esc=Cancel  Tab=Next)")
            safe_addstr(win, dh - 2, 2,
                        "Enter/s:Save  Esc:Cancel  Tab:Next  Shift-Tab:Prev",
                        curses.color_pair(CP_QRT))
            if focus < offset: offset = focus
            if focus >= offset + visible: offset = focus - visible + 1
            for i, (key, label, tf) in enumerate(fields[offset:offset + visible]):
                row = i + 1
                idx = offset + i
                lattr = curses.color_pair(CP_LABEL) | (curses.A_BOLD if idx == focus else 0)
                safe_addstr(win, row, 2, f"{label}:", lattr)
                tf.draw(win, row, 20, dw - 22, focused=(idx == focus))
            win.refresh()
            ch = win.getch()
            if ch == 27: return None
            if ch in (ord('\n'), ord('\r'), ord('s')):
                for key, _, tf in fields:
                    new_cfg[key] = tf.value()
                return new_cfg
            if ch == ord('\t'):
                focus = (focus + 1) % len(fields)
            elif ch == curses.KEY_BTAB:
                focus = (focus - 1) % len(fields)
            else:
                fields[focus][2].handle_key(ch)

    @staticmethod
    def logbook_menu(stdscr, current_path: Optional[pathlib.Path]) -> Optional[dict]:
        H, W = stdscr.getmaxyx()
        dw = min(60, W - 4); dh = 10
        y0 = (H - dh) // 2; x0 = (W - dw) // 2
        win = curses.newwin(dh, dw, y0, x0)
        draw_border(win, "Logbook")
        options = [
            ("n", "New logbook"),
            ("o", "Open existing .adi file"),
            ("c", "Close current logbook"),
        ]
        cur_str = str(current_path) if current_path else "(none)"
        safe_addstr(win, 1, 2, f"Current: {trunc(cur_str, dw-12)}", curses.color_pair(CP_NORMAL))
        for i, (key, label) in enumerate(options):
            safe_addstr(win, i + 3, 2, f"  {key}: {label}", curses.color_pair(CP_LABEL))
        safe_addstr(win, dh - 2, 2, "Esc=Cancel", curses.color_pair(CP_QRT))
        win.refresh()
        while True:
            ch = win.getch()
            if ch == 27: return None
            if ch == ord('n'): return {"action": "new"}
            if ch == ord('o'): return {"action": "open"}
            if ch == ord('c'): return {"action": "close"}

    @staticmethod
    def spot_post_dialog(stdscr, cfg: dict, flrig: FlrigState,
                         spot: Optional[dict]) -> Optional[dict]:
        H, W = stdscr.getmaxyx()
        dw = min(60, W - 4); dh = 12
        y0 = (H - dh) // 2; x0 = (W - dw) // 2
        win = curses.newwin(dh, dw, y0, x0)
        draw_border(win, "Post Spot to POTA")

        freq_khz = f"{flrig.freq * 1000:.1f}" if flrig.freq else ""
        mode     = flrig.mode or ""
        fields = [
            ("Activator", TextField(15, True)),
            ("Park Ref",  TextField(10, True)),
            ("Freq kHz",  TextField(10, False)),
            ("Mode",      TextField(8,  True)),
            ("Comment",   TextField(40, False)),
        ]
        defaults = [cfg.get("callsign",""), "", freq_khz, mode, ""]
        if spot:
            defaults[0] = str(spot.get("activator","") or "")
            defaults[1] = str(spot.get("reference","") or "")
            defaults[2] = str(spot.get("frequency","") or freq_khz)
            defaults[3] = str(spot.get("mode","") or mode)
        for (_, tf), val in zip(fields, defaults):
            tf.set_value(val)
        focus = 0
        while True:
            win.erase()
            draw_border(win, "Post Spot  Enter/s=Send  Esc=Cancel")
            for i, (label, tf) in enumerate(fields):
                lattr = curses.color_pair(CP_LABEL) | (curses.A_BOLD if i==focus else 0)
                safe_addstr(win, i+1, 2, f"{label}:", lattr)
                tf.draw(win, i+1, 14, dw-16, focused=(i==focus))
            safe_addstr(win, dh-2, 2, "Enter/s:Send  Esc:Cancel  Tab:Next", curses.color_pair(CP_QRT))
            win.refresh()
            ch = win.getch()
            if ch == 27: return None
            if ch in (ord('\n'), ord('\r'), ord('s')):
                return {k: tf.value() for (k, tf) in [(f[0].lower().replace(" ","_"), f[1]) for f in fields]}
            if ch == ord('\t'):
                focus = (focus+1) % len(fields)
            elif ch == curses.KEY_BTAB:
                focus = (focus-1) % len(fields)
            else:
                fields[focus][1].handle_key(ch)

    @staticmethod
    def help_overlay(stdscr):
        lines = [
            "POTA Hunter TUI — Keyboard Reference",
            "",
            "F2 / Enter  Log QSO (stamps UTC, reads Flrig freq/mode)",
            "F3          Tune Flrig to selected spot",
            "F4          Respot selected activator to POTA API",
            "F5          Force-refresh POTA spot list",
            "F6          Toggle auto-scan on/off",
            "F7          Logbook menu (new / open / close)",
            "F8          Post my own activation spot",
            "F9          Open config editor",
            "F10         Database tools (build Parks / FCC callsign DB)",
            "F12         Toggle Activator mode (locks park, rapid logging)",
            "F1          Cycle focus: form → spots → log → form",
            "Tab/Down    Next field in QSO form",
            "Shift-Tab/Up  Previous field in QSO form",
            "Enter       (spot list) Tune + fill form",
            "Arrow keys  Navigate spot list or QSO log",
            "PgUp/PgDn   Scroll by 10 / 5 rows",
            "b           Cycle band filter",
            "m           Cycle mode filter",
            "r           Cycle ITU region filter",
            "x           Toggle hide QRT spots",
            "o           Toggle hide out-of-band spots",
            "Ctrl-R      Force full screen redraw",
            "?           This help",
            "q           Quit",
        ]
        ModalDialog.message(stdscr, "Help", "\n".join(lines))

    @staticmethod
    def db_tools_menu(stdscr) -> Optional[dict]:
        H, W = stdscr.getmaxyx()
        dw = min(50, W - 4); dh = 11
        y0 = (H - dh) // 2; x0 = (W - dw) // 2
        win = curses.newwin(dh, dw, y0, x0)
        draw_border(win, "Database Tools")

        # Collect status strings
        try:
            parks_count = sqlite3.connect(PARKS_DB).execute(
                "SELECT COUNT(*) FROM parks").fetchone()[0] if PARKS_DB.exists() else 0
        except Exception:
            parks_count = 0
        try:
            fcc_count = sqlite3.connect(FCC_DB).execute(
                "SELECT COUNT(*) FROM callsigns").fetchone()[0] if FCC_DB.exists() else 0
        except Exception:
            fcc_count = 0

        parks_status = f"✓ {parks_count:,} parks" if parks_count else "✗ not built"
        fcc_status   = f"✓ {fcc_count:,} calls" if fcc_count else "✗ not built"

        safe_addstr(win, 1, 2, f"Parks DB: {parks_status}", curses.color_pair(
            CP_SUCCESS if parks_count else CP_ERROR))
        safe_addstr(win, 2, 2, f"FCC DB:   {fcc_status}", curses.color_pair(
            CP_SUCCESS if fcc_count else CP_ERROR))
        safe_addstr(win, 4, 2, "p: Build/Update Parks DB",
                    curses.color_pair(CP_LABEL) | curses.A_BOLD)
        safe_addstr(win, 5, 2, "f: Build/Update FCC Callsign DB",
                    curses.color_pair(CP_LABEL) | curses.A_BOLD)
        safe_addstr(win, 6, 2, "   (FCC download is ~100 MB, takes ~2 min)",
                    curses.color_pair(CP_QRT))
        safe_addstr(win, dh - 2, 2, "Esc=Cancel", curses.color_pair(CP_QRT))
        win.refresh()
        while True:
            ch = win.getch()
            if ch == 27: return None
            if ch == ord('p'): return {"action": "parks"}
            if ch == ord('f'): return {"action": "fcc"}


# ── Section 7: App controller ─────────────────────────────────────────────────

FOCUS_SPOTS  = "spots"
FOCUS_FORM   = "form"
FOCUS_LOG    = "log"
FOCUS_FILTER = "filter"

class PotaHunterTUI:

    def __init__(self, stdscr, cfg: dict):
        self.stdscr       = stdscr
        self.cfg          = cfg
        self.flrig_state  = FlrigState()
        self.spot_state   = SpotState()
        self.qso_index    = make_index()
        self.logbook_path: Optional[pathlib.Path] = None
        self.lookup:       Optional[LookupResult]  = None
        self.park_info:    Optional[ParkResult]    = None
        self.event_queue: queue.Queue = queue.Queue(maxsize=300)
        self.status_msg   = StatusMessage("Ready", "ok")
        self.status_exp   = 0.0      # monotonic time when status expires
        self.focus          = FOCUS_SPOTS
        self._scan_enabled  = False
        self._activator_mode= False

        # Workers (started in run())
        self.flrig_poller   = FlrigPoller(self.event_queue, cfg)
        self.spot_refresher = SpotRefresher(self.event_queue, cfg, self.spot_state)
        self.lookup_worker  = LookupWorker(self.event_queue, cfg)
        self.auto_scanner   = AutoScanner(self.event_queue, self.spot_state, cfg)

        # Widgets (created in layout())
        self.w_status:   Optional[StatusBar]     = None
        self.w_spots:    Optional[SpotListPanel] = None
        self.w_form:     Optional[QSOForm]       = None
        self.w_log:      Optional[QSOLogPanel]   = None
        self.w_filter:   Optional[FilterBar]     = None
        self.w_help:     Optional[HelpBar]       = None

        # Open last logbook if set
        last = cfg.get("last_logbook", "")
        if last and pathlib.Path(last).exists():
            self._open_logbook(pathlib.Path(last))

        if not parks_db_exists():
            self._set_status("Parks DB not found — press F10 to build it.", "warn")

    # ── Layout ────────────────────────────────────────────────────────────────

    def layout(self):
        H, W = self.stdscr.getmaxyx()
        if H < 24 or W < 80:
            return  # guard handled in run()

        # Row allocation
        STATUS_H = 2
        FILTER_H = 1
        HELP_H   = 1
        MIDDLE_H = H - STATUS_H - FILTER_H - HELP_H

        # Column split
        if W >= 110:
            FORM_W = 34
        elif W >= 95:
            FORM_W = 32
        else:
            FORM_W = 30
        SPOTS_W = W - FORM_W

        SPOTS_H = MIDDLE_H * 2 // 3
        LOG_H   = MIDDLE_H - SPOTS_H

        row0 = 0
        row1 = STATUS_H
        row2 = row1 + SPOTS_H
        row3 = row2 + LOG_H
        row4 = row3 + FILTER_H

        self._status_win = curses.newwin(STATUS_H, W, row0, 0)
        self._spots_win  = curses.newwin(SPOTS_H,  SPOTS_W, row1, 0)
        self._form_win   = curses.newwin(MIDDLE_H, FORM_W, row1, SPOTS_W)
        self._log_win    = curses.newwin(LOG_H,    W, row2, 0)
        self._filter_win = curses.newwin(FILTER_H + 1, W, row3, 0)
        self._help_win   = curses.newwin(HELP_H,   W, row4, 0)

        self.w_status  = StatusBar(self._status_win)
        self.w_spots   = SpotListPanel(self._spots_win)
        self.w_form    = QSOForm(self._form_win)
        self.w_log     = QSOLogPanel(self._log_win)
        self.w_filter  = FilterBar(self._filter_win)
        self.w_help    = HelpBar(self._help_win)

        # Restore spots / qsos into new widgets
        spots    = self.spot_state.get_filtered()
        worked   = self._worked_calls()
        self.w_spots.set_spots(spots, worked)
        qsos = self.qso_index.execute(
            "SELECT * FROM qso ORDER BY date DESC, time_on DESC LIMIT 200").fetchall()
        self.w_log.set_qsos(qsos)

    def _worked_calls(self) -> set:
        rows = self.qso_index.execute("SELECT DISTINCT call FROM qso").fetchall()
        return {r[0].upper() for r in rows}

    # ── Main run loop ─────────────────────────────────────────────────────────

    def run(self):
        curses.curs_set(0)
        self.stdscr.keypad(True)
        setup_colors()
        curses.halfdelay(2)  # 200ms getch timeout

        self.layout()

        self.flrig_poller.start()
        self.spot_refresher.start()
        self.lookup_worker.start()
        self.auto_scanner.start()

        while True:
            H, W = self.stdscr.getmaxyx()
            if H < 24 or W < 80:
                self._draw_too_small(H, W)
                ch = self.stdscr.getch()
                if ch == ord('q'):
                    break
                continue

            self._process_events()
            self._check_idle_lookup()
            self._redraw()

            ch = self.stdscr.getch()
            if ch == curses.ERR:
                continue
            if ch == curses.KEY_RESIZE:
                curses.update_lines_cols()
                self.stdscr.clear()
                self.layout()
                continue
            if self._dispatch_global(ch):
                continue
            self._dispatch_focus(ch)

    def _draw_too_small(self, H, W):
        self.stdscr.erase()
        msg = f"Terminal too small ({W}x{H}) — need 80x24 minimum. Resize or press q to quit."
        safe_addstr(self.stdscr, 0, 0, msg[:W], curses.color_pair(CP_ERROR))
        self.stdscr.refresh()

    # ── Event processing ──────────────────────────────────────────────────────

    def _process_events(self):
        while True:
            try:
                ev = self.event_queue.get_nowait()
            except queue.Empty:
                break
            if isinstance(ev, EvFlrigUpdate):
                self.flrig_state.update(
                    ev.freq, ev.mode, ev.smeter, ev.pwr, ev.ptt, ev.connected)
            elif isinstance(ev, EvSpotUpdate):
                worked = self._worked_calls()
                if self.w_spots:
                    self.w_spots.set_spots(ev.filtered, worked)
            elif isinstance(ev, EvLookupResult):
                self.lookup = ev.result
                # Also trigger park lookup if park field is filled
                if self.w_form:
                    park_ref = self.w_form.park_value()
                    if park_ref:
                        self._lookup_park(park_ref)
            elif isinstance(ev, EvParkResult):
                self.park_info = ev.result
            elif isinstance(ev, EvStatus):
                self.status_msg = ev.msg
                self.status_exp = time.monotonic() + 5.0
            elif isinstance(ev, EvTuneRequest):
                self._do_tune(ev.freq_hz, ev.mode)

    def _check_idle_lookup(self):
        if self.w_form and self.w_form.should_trigger_lookup():
            call = self.w_form.callsign_value()
            if call:
                self.lookup_worker.trigger(call)
                self.lookup = None
                self.park_info = None
                # Also look up park if filled
                park = self.w_form.park_value()
                if park:
                    self._lookup_park(park)

    def _lookup_park(self, ref: str):
        def _run():
            p = lookup_park(ref)
            if p:
                pr = ParkResult(
                    ref=p.get("reference",""),
                    name=p.get("name",""),
                    state=p.get("state",""),
                    country=p.get("country",""),
                    grid=p.get("grid",""),
                )
                try:
                    self.event_queue.put_nowait(EvParkResult(result=pr))
                except queue.Full:
                    pass
        threading.Thread(target=_run, daemon=True).start()

    # ── Drawing ───────────────────────────────────────────────────────────────

    def _redraw(self):
        fs = self.flrig_state.snapshot()
        spots = self.spot_state.get_filtered()

        if time.monotonic() > self.status_exp:
            self.status_msg = StatusMessage("", "info")

        if self.w_status:
            self.w_status.draw(fs, self.cfg, self.logbook_path,
                               len(spots), self.status_msg,
                               activator_mode=self._activator_mode)
        if self.w_spots:
            self.w_spots.draw(self.cfg)
        if self.w_form:
            self.w_form.draw(self.lookup, self.park_info)
        if self.w_log:
            self.w_log.draw()
        if self.w_filter:
            self.w_filter.draw(self.cfg, self._scan_enabled)
        if self.w_help:
            self.w_help.draw()
        curses.doupdate()

    # ── Key dispatch ──────────────────────────────────────────────────────────

    def _dispatch_global(self, ch: int) -> bool:
        if (ch == ord('q') or ch == ord('Q')) and self.focus != FOCUS_FORM:
            if ModalDialog.confirm(self.stdscr, "Quit", "Quit POTA Hunter TUI?"):
                self._shutdown()
                raise SystemExit(0)
            return True
        if ch == curses.KEY_F5 or ch == ord('!'):
            self.cmd_refresh_spots(); return True
        if ch == curses.KEY_F6:
            self.cmd_toggle_scan(); return True
        if ch == curses.KEY_F7:
            self.cmd_logbook_menu(); return True
        if ch == curses.KEY_F8:
            self.cmd_post_my_spot(); return True
        if ch == curses.KEY_F9:
            self.cmd_config(); return True
        if ch == curses.KEY_F10:
            self.cmd_db_tools(); return True
        if ch == curses.KEY_F12:
            self.cmd_toggle_activator_mode(); return True
        if ch == curses.KEY_F1:
            self._cycle_focus(forward=True); return True
        if ch == ord('?') and self.focus != FOCUS_FORM:
            ModalDialog.help_overlay(self.stdscr); return True
        if ch == 18:  # Ctrl-R
            self.stdscr.clear(); self.layout(); return True
        return False

    def _dispatch_focus(self, ch: int):
        if self.focus == FOCUS_SPOTS and self.w_spots:
            if ch == ord('\n') or ch == ord('\r'):
                spot = self.w_spots.selected_spot()
                if spot:
                    self.cmd_tune_to_spot(spot)
                    if self.w_form:
                        self.w_form.populate_from_spot(spot)
                        self.lookup_worker.trigger(
                            str(spot.get("activator","") or ""))
                return
            if ch == curses.KEY_F2:
                spot = self.w_spots.selected_spot()
                if spot and self.w_form:
                    self.w_form.populate_from_spot(spot)
                self.focus = FOCUS_FORM; return
            if ch == curses.KEY_F3:
                spot = self.w_spots.selected_spot()
                if spot: self.cmd_tune_to_spot(spot); return
            if ch == curses.KEY_F4:
                spot = self.w_spots.selected_spot()
                if spot: self.cmd_respot(spot); return
            if self.w_filter and self.w_filter.handle_key(ch, self.cfg):
                self.spot_refresher.trigger_now(); return
            self.w_spots.handle_key(ch)

        elif self.focus == FOCUS_FORM and self.w_form:
            result = self.w_form.handle_key(ch)
            if result == "log":
                self.cmd_log_qso()
            elif result == "clear":
                self.w_form.clear(); self.lookup = None; self.park_info = None
            elif result == "next-panel":
                self.focus = FOCUS_SPOTS
            if ch == curses.KEY_F3:
                spot = self.w_spots.selected_spot() if self.w_spots else None
                if spot: self.cmd_tune_to_spot(spot)
            if ch == curses.KEY_F4:
                spot = self.w_spots.selected_spot() if self.w_spots else None
                if spot: self.cmd_respot(spot)

        elif self.focus == FOCUS_LOG and self.w_log:
            self.w_log.handle_key(ch)

    def _cycle_focus(self, forward: bool):
        order = [FOCUS_FORM, FOCUS_SPOTS, FOCUS_LOG]
        idx   = order.index(self.focus) if self.focus in order else 0
        step  = 1 if forward else -1
        self.focus = order[(idx + step) % len(order)]

    # ── Commands ──────────────────────────────────────────────────────────────

    def cmd_log_qso(self):
        if not self.logbook_path:
            action = ModalDialog.logbook_menu(self.stdscr, None)
            if not action or not self._handle_logbook_action(action):
                return
        if not self.w_form:
            return
        vals = self.w_form.get_values()
        if not vals["call"]:
            self._set_status("Callsign is required.", "warn"); return

        now = datetime.datetime.now(datetime.timezone.utc)
        fs  = self.flrig_state.snapshot()

        freq = fs.freq if fs.connected and fs.freq else None
        mode = fs.mode if fs.connected and fs.mode else ""
        band = freq_to_band(freq) if freq else ""

        lu_name = self.lookup.name if self.lookup else ""
        lu_qth  = self.lookup.qth  if self.lookup else ""
        lu_grid = self.lookup.grid if self.lookup else ""

        self.qso_index.execute("""
            INSERT INTO qso (call,date,time_on,freq,band,mode,
                rst_sent,rst_rcvd,name,qth,gridsquare,park_nr,comment,notes)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            vals["call"].upper(),
            now.strftime("%Y-%m-%d"),
            now.strftime("%H%M"),
            freq, band, mode,
            vals["rst_sent"] or "59",
            vals["rst_rcvd"] or "59",
            lu_name, lu_qth, lu_grid,
            vals["park_nr"],
            vals["comment"],
            vals["notes"],
        ))
        self.qso_index.commit()
        rewrite_adif(self.logbook_path, self.qso_index,
                     self.cfg.get("callsign", ""))

        qsos = self.qso_index.execute(
            "SELECT * FROM qso ORDER BY date DESC, time_on DESC LIMIT 200").fetchall()
        if self.w_log:
            self.w_log.set_qsos(qsos)
        if self.w_spots:
            self.w_spots.set_spots(self.spot_state.get_filtered(), self._worked_calls())

        self._set_status(f"Logged {vals['call']}  {band} {mode}", "ok")
        self.w_form.clear()
        self.lookup = None; self.park_info = None

    def cmd_tune_to_spot(self, spot: dict):
        freq_khz_str = str(spot.get("frequency", "") or "")
        mode         = str(spot.get("mode", "") or "")
        if not freq_khz_str:
            self._set_status("Spot has no frequency.", "warn"); return
        try:
            freq_hz = float(freq_khz_str) * 1000
        except ValueError:
            self._set_status("Invalid frequency.", "warn"); return
        host = self.cfg.get("flrig_host", "127.0.0.1")
        port = int(self.cfg.get("flrig_port", 12345))
        result = flrig_set_freq_and_mode(host, port, freq_hz, mode)
        if result is True:
            mhz = freq_hz / 1_000_000
            self._set_status(f"Tuned to {mhz:.3f} MHz {mode}", "ok")
        else:
            self._set_status(f"Tune failed: {result}", "error")

    def cmd_respot(self, spot: dict):
        res = ModalDialog.spot_post_dialog(
            self.stdscr, self.cfg, self.flrig_state.snapshot(), spot)
        if not res:
            return
        activator = res.get("activator", "")
        park_ref  = res.get("park_ref", "")
        freq_khz  = res.get("freq_khz", "")
        mode      = res.get("mode", "")
        comment   = res.get("comment", "")
        spotter   = self.cfg.get("callsign", "")
        ok, err = pota_post_spot(activator, spotter, park_ref, freq_khz, mode, comment)
        if ok:
            self._set_status(f"Respot sent for {activator}", "ok")
        else:
            self._set_status(f"Respot failed: {err}", "error")

    def cmd_refresh_spots(self):
        self._set_status("Refreshing spots…", "info")
        self.spot_refresher.trigger_now()

    def cmd_toggle_scan(self):
        self._scan_enabled = not self._scan_enabled
        if self._scan_enabled:
            self.auto_scanner.enable()
            self._set_status("Auto-scan enabled.", "ok")
        else:
            self.auto_scanner.disable()
            self._set_status("Auto-scan disabled.", "info")

    def cmd_toggle_activator_mode(self):
        self._activator_mode = not self._activator_mode
        if self._activator_mode:
            my_park = self.cfg.get("my_park", "").strip()
            if not my_park:
                my_park = ModalDialog.input_line(
                    self.stdscr, "Activator Mode",
                    "Enter your park reference (e.g. K-0042):", "")
                if not my_park:
                    self._activator_mode = False
                    return
                self.cfg["my_park"] = my_park.strip().upper()
                save_config(self.cfg)
            self.focus = FOCUS_FORM
            self._set_status(
                f"ACTIVATOR MODE ON — Park: {self.cfg['my_park']}", "ok")
        else:
            if self.w_form:
                self.w_form.fields[3].set_value("")
            self._set_status("Activator mode OFF — Hunter mode", "info")

    def cmd_logbook_menu(self):
        action = ModalDialog.logbook_menu(self.stdscr, self.logbook_path)
        if action:
            self._handle_logbook_action(action)

    def _handle_logbook_action(self, action: dict) -> bool:
        a = action.get("action", "")
        if a == "new":
            name = ModalDialog.input_line(
                self.stdscr, "New Logbook", "Filename (without .adi):",
                datetime.datetime.now().strftime("pota_%Y%m%d"))
            if not name:
                return False
            p = LOGBOOK_DIR / (name.strip() + ".adi")
            self._open_logbook(p, create=True)
            return True
        if a == "open":
            # Simple: list .adi files in LOGBOOK_DIR
            adi_files = sorted(LOGBOOK_DIR.glob("*.adi"))
            if not adi_files:
                ModalDialog.message(self.stdscr, "Open",
                                    "No .adi files found in POTA-Hunter folder.")
                return False
            lines = [f"  {i+1}. {f.name}" for i, f in enumerate(adi_files)]
            lines.append("\nEnter number:")
            resp = ModalDialog.input_line(self.stdscr, "Open Logbook",
                                          "\n".join(lines[:15]))
            if resp and resp.strip().isdigit():
                idx = int(resp.strip()) - 1
                if 0 <= idx < len(adi_files):
                    self._open_logbook(adi_files[idx]); return True
            return False
        if a == "close":
            self.logbook_path = None
            self.qso_index.execute("DELETE FROM qso"); self.qso_index.commit()
            if self.w_log: self.w_log.set_qsos([])
            self._set_status("Logbook closed.", "info")
            return True
        return False

    def _open_logbook(self, path: pathlib.Path, create: bool = False):
        n = load_adif_into_index(path, self.qso_index)
        self.logbook_path = path
        self.cfg["last_logbook"] = str(path)
        qsos = self.qso_index.execute(
            "SELECT * FROM qso ORDER BY date DESC, time_on DESC LIMIT 200").fetchall()
        if self.w_log:
            self.w_log.set_qsos(qsos)
        if self.w_spots:
            self.w_spots.set_spots(self.spot_state.get_filtered(), self._worked_calls())
        self._set_status(f"Logbook: {path.name}  ({n} QSOs loaded)", "ok")

    def cmd_post_my_spot(self):
        fs = self.flrig_state.snapshot()
        res = ModalDialog.spot_post_dialog(self.stdscr, self.cfg, fs, None)
        if not res:
            return
        activator = res.get("activator", "") or self.cfg.get("callsign", "")
        park_ref  = res.get("park_ref", "")
        freq_khz  = res.get("freq_khz", "")
        mode      = res.get("mode", "")
        comment   = res.get("comment", "")
        spotter   = self.cfg.get("callsign", "")
        ok, err   = pota_post_spot(activator, spotter, park_ref, freq_khz, mode, comment)
        if ok:
            self._set_status(f"Spot posted for {activator} @ {park_ref}", "ok")
        else:
            self._set_status(f"Spot post failed: {err}", "error")

    def cmd_config(self):
        new_cfg = ModalDialog.config_editor(self.stdscr, self.cfg)
        if new_cfg is not None:
            self.cfg.update(new_cfg)
            save_config(self.cfg)
            # Re-login QRZ if credentials changed
            if self.cfg.get("qrz_user") and self.cfg.get("qrz_pass"):
                threading.Thread(
                    target=lambda: qrz_login(self.cfg["qrz_user"], self.cfg["qrz_pass"]),
                    daemon=True).start()
            self._set_status("Configuration saved.", "ok")

    def cmd_db_tools(self):
        res = ModalDialog.db_tools_menu(self.stdscr)
        if not res:
            return
        action = res.get("action")
        if action == "parks":
            label = "Parks"
            build_fn = build_parks_db
        elif action == "fcc":
            label = "FCC"
            build_fn = build_fcc_db
        else:
            return

        self._set_status(f"{label} DB build started… watch status bar", "info")

        def _run():
            def cb(msg):
                try:
                    self.event_queue.put_nowait(
                        EvStatus(StatusMessage(msg, "info")))
                except queue.Full:
                    pass
            count, err = build_fn(progress_cb=cb)
            if err:
                final = StatusMessage(f"{label} DB build failed: {err}", "error")
            else:
                final = StatusMessage(
                    f"{label} DB ready — {count:,} records.", "ok")
            try:
                self.event_queue.put_nowait(EvStatus(final))
            except queue.Full:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def _set_status(self, text: str, level: str = "info"):
        self.status_msg = StatusMessage(text, level)
        self.status_exp = time.monotonic() + 6.0

    def _shutdown(self):
        save_config(self.cfg)
        self.flrig_poller.stop()
        self.spot_refresher.stop()
        self.lookup_worker.stop()
        self.auto_scanner.stop()

    def _do_tune(self, freq_hz: float, mode: str):
        host = self.cfg.get("flrig_host", "127.0.0.1")
        port = int(self.cfg.get("flrig_port", 12345))
        flrig_set_freq_and_mode(host, port, freq_hz, mode)


# ── Section 8: Entry point ────────────────────────────────────────────────────

def main():
    cfg = load_config()

    # Attempt QRZ login in background if credentials exist
    if cfg.get("qrz_user") and cfg.get("qrz_pass"):
        threading.Thread(
            target=lambda: qrz_login(cfg["qrz_user"], cfg["qrz_pass"]),
            daemon=True).start()

    try:
        curses.wrapper(lambda stdscr: PotaHunterTUI(stdscr, cfg).run())
    except SystemExit:
        pass
    finally:
        save_config(cfg)

if __name__ == "__main__":
    main()
