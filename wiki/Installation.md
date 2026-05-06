# Installation

POTA Hunter requires **Python 3.6 or later** and uses only Python's standard library. There is no `pip install` step.

---

## Windows

### Option A — Pre-built Executable (Recommended)

1. Download `POTA-Logger.exe` from the [Releases page](https://github.com/abogue77/POTA-Logger/releases).
2. Place it anywhere on your computer (e.g., `C:\Ham Radio\POTA-Logger.exe`).
3. Double-click to run. No installation required.

### Option B — Run from Source

1. Install [Python 3.6+](https://www.python.org/downloads/) (check **Add Python to PATH** during setup).
2. Download or clone this repository.
3. Double-click `potahunter.pyw`, or open a terminal and run:

```powershell
python potahunter.pyw
```

---

## Linux

### Automatic Installer

```bash
git clone https://github.com/abogue77/POTA-Logger.git
cd POTA-Logger
bash install.sh
```

The installer:
- Copies the app to `~/.local/share/potahunter/`
- Creates a launcher at `~/.local/bin/potahunter`
- Adds a desktop entry for your application menu

After installation, launch with:

```bash
potahunter
```

### Prerequisites

```bash
# Debian / Ubuntu / Raspberry Pi OS
sudo apt install python3 python3-tk

# Fedora
sudo dnf install python3 python3-tkinter

# Arch
sudo pacman -S python tk
```

### Manual Run (without installing)

```bash
python3 potahunter.pyw
```

---

## macOS

```bash
# Install Python if needed (Homebrew recommended)
brew install python python-tk

git clone https://github.com/abogue77/POTA-Logger.git
cd POTA-Logger
python3 potahunter.pyw
```

---

## Data Storage

On first launch, POTA Hunter creates a `HamLog/` folder in your home directory:

```
~/HamLog/
├── config.json        # Your settings (callsign, QRZ, Flrig, etc.)
├── pota_parks.db      # Local copy of the POTA parks database
└── *.adi              # Your ADIF logbook files
```

You can back up this folder to preserve all your logs and settings.

---

## Updating

**Windows EXE**: Download the new release and replace the old `.exe` file. Your `~/HamLog/` data is untouched.

**From source**: `git pull` inside the cloned folder, then restart the app.

**Parks database**: Use **File → Update Parks DB** inside the app to refresh the POTA parks list from pota.app.
