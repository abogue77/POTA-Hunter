# Building from Source

---

## Prerequisites

| Platform | Requirements |
|----------|-------------|
| Windows | Python 3.6+, PyInstaller |
| Linux | Python 3.6+, python3-tk |
| macOS | Python 3.6+, python-tk (via Homebrew) |

POTA Hunter has **no third-party Python dependencies** beyond PyInstaller (which is only needed to build the Windows EXE, not to run the app).

---

## Running from Source (All Platforms)

No build step is required to run the app from source.

**Windows:**
```powershell
python potahunter.pyw
```

**Linux / macOS:**
```bash
python3 potahunter.pyw
```

---

## Building the Windows Executable

The build produces a single-file, standalone `POTA-Logger.exe` that includes Python and all standard library modules. Users do not need Python installed to run it.

### 1. Install PyInstaller

```powershell
pip install pyinstaller
```

### 2. Run the Build Script

```batch
build_windows.bat
```

Or manually:

```powershell
pyinstaller POTA-Logger.spec
```

### 3. Output

The compiled executable is placed at:
```
dist\POTA-Logger.exe
```

The `build\` folder contains intermediate files and can be deleted after a successful build.

### PyInstaller Spec File

The `POTA-Logger.spec` file configures the build. Key settings:
- `onefile = True` — single EXE output
- `windowed = True` — no console window
- Entry point: `potahunter.pyw`

---

## Linux / macOS Installer

The included `install.sh` script handles installation for Linux and macOS:

```bash
bash install.sh
```

What it does:
1. Copies `potahunter.pyw` to `~/.local/share/potahunter/`
2. Creates a launcher script at `~/.local/bin/potahunter`
3. Writes a `.desktop` entry for the application menu (Linux only)

After installation:
```bash
potahunter
```

To uninstall:
```bash
rm -rf ~/.local/share/potahunter/
rm ~/.local/bin/potahunter
rm ~/.local/share/applications/potahunter.desktop  # Linux only
```

---

## Project Structure

```
POTA-Logger/
├── potahunter.pyw           # Entire application — single Python file
├── POTA-Logger.spec     # PyInstaller build configuration
├── build_windows.bat    # Windows build script
├── install.sh           # Linux/macOS installer
├── README.md
├── build/               # PyInstaller intermediate output (gitignored)
└── dist/                # Final build output (gitignored)
    └── POTA-Logger.exe
```

All application code is in the single file `potahunter.pyw`. There are no submodules or packages.

---

## Contributing

1. Fork the repository on GitHub.
2. Make your changes to `potahunter.pyw`.
3. Test by running `python potahunter.pyw` (or `python3 potahunter.pyw` on Linux/macOS).
4. Submit a pull request with a description of your changes.

Please test on Windows if possible before submitting, since the primary release target is the Windows EXE.
