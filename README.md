# POTA Hunter

**POTA Activator Hunter & Ham Radio Logger**

A Python-based desktop application for amateur radio operators participating in [Parks on the Air (POTA)](https://parksontheair.com/). Monitors live POTA spots, controls your transceiver via Flrig, logs QSOs, and visualizes worked grid squares — all in one window.

---

## Features

### Live POTA Spot Monitoring
- Pulls real-time activator spots from the POTA API
- Filter spots by band and mode
- Configurable auto-scan interval
- Tracks which stations you have already worked

### Transceiver Control (Flrig)
- Connects to [Flrig](http://www.w1hkj.com/files/flrig/) via XML-RPC
- Displays live VFO frequency, mode, and band
- S-meter and power meter
- PTT state detection

### QSO Logging
- UTC timestamp auto-fill
- Fields for callsign, RST sent/received, park reference, comments, and notes
- ADIF import and export
- SQLite-backed fast search, filter, and sort by callsign, band, mode, or date

### Park Database
- Synced from the official POTA parks CSV
- Park reference lookup with location data
- Automatic database updates on launch

### QRZ Callsign Lookup
- Auto-populates name, location, and grid square via QRZ.com XML API

### Grid Square Map
- Maidenhead grid square visualization with world coastlines
- Interactive hover tooltips
- Browser-based map export

---

## Screenshots

_Add screenshots here_

---

## Requirements

- Python 3.6 or later
- Tkinter (included with most Python installations)
- Flrig (optional, for transceiver control)
- QRZ.com account (optional, for callsign lookup)

---

## Installation

### Windows

Download the latest `POTA-Logger.exe` from the [Releases](https://github.com/abogue77/POTA-Hunter/releases) page and run it — no Python installation required.

To build from source:

```bat
git clone https://github.com/abogue77/POTA-Hunter.git
cd POTA-Hunter
build_windows.bat
```

The executable will be output to `dist\POTA-Logger.exe`.

### Linux

```bash
git clone https://github.com/abogue77/POTA-Hunter.git
cd POTA-Hunter
./install.sh
```

The installer supports apt, dnf, pacman, and zypper. It installs the app to `~/.local/share/potahunter/`, adds a launcher to `~/.local/bin/`, and creates a desktop entry. Logs are stored in `~/HamLog/`.

### Run from Source (any platform)

```bash
python potahunter.pyw
```

---

## Configuration

On first launch, open **Settings** to configure:

| Setting | Description |
|---|---|
| Callsign | Your station callsign |
| Grid Square | Your Maidenhead grid square |
| QRZ Username / Password | For callsign lookups |
| Flrig Host / Port | Default: `localhost:12345` |
| Default Logbook Path | Where `.adi` log files are saved |
| Theme | Light or dark |

Settings are saved to a JSON config file and restored on next launch.

---

## Usage

1. Launch the app and configure your station settings.
2. Connect Flrig if you want live radio control.
3. Open the **POTA Spots** tab to watch for active activators.
4. Click a spot to QSY and pre-fill the log entry form.
5. Complete the QSO and click **Log** to save it.
6. View worked grid squares on the **Grid Map** tab.
7. Export your log via **File → Export ADIF** when done.

---

## License

See [LICENSE](LICENSE) for details.

---

## Contributing

Issues and pull requests are welcome. Please open an issue first for any significant changes.
