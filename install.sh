#!/usr/bin/env bash
set -euo pipefail

APP_NAME="hamlog"
INSTALL_DIR="$HOME/.local/share/hamlog"
BIN_DIR="$HOME/.local/bin"
DESKTOP_DIR="$HOME/.local/share/applications"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "=== POTA Logger Installer ==="
echo ""

# 1. Check Python 3
if ! command -v python3 &>/dev/null; then
    echo "ERROR: python3 not found. Please install Python 3.6 or later and re-run this script."
    exit 1
fi

PY_VERSION=$(python3 -c "import sys; print(sys.version_info.minor + sys.version_info.major * 10)")
if [ "$PY_VERSION" -lt 36 ]; then
    echo "ERROR: Python 3.6 or later is required. Found: $(python3 --version)"
    exit 1
fi

echo "Python 3 found: $(python3 --version)"

# 2. Check Tkinter; install python3-tk if missing
if ! python3 -c "import tkinter" &>/dev/null 2>&1; then
    echo "Tkinter not found. Attempting to install..."

    if command -v apt-get &>/dev/null; then
        sudo apt-get install -y python3-tk
    elif command -v dnf &>/dev/null; then
        sudo dnf install -y python3-tkinter
    elif command -v pacman &>/dev/null; then
        sudo pacman -S --noconfirm tk
    elif command -v zypper &>/dev/null; then
        sudo zypper install -y python3-tk
    else
        echo "WARNING: Could not detect a supported package manager (apt/dnf/pacman/zypper)."
        echo "         Please install python3-tk (or equivalent) manually, then re-run this script."
        exit 1
    fi

    # Verify install succeeded
    if ! python3 -c "import tkinter" &>/dev/null 2>&1; then
        echo "ERROR: Tkinter installation failed. Please install python3-tk manually."
        exit 1
    fi
fi

echo "Tkinter OK"

# 3. Create install directories
mkdir -p "$INSTALL_DIR" "$BIN_DIR" "$DESKTOP_DIR"

# 4. Copy application file
cp "$SCRIPT_DIR/hamlog.pyw" "$INSTALL_DIR/hamlog.pyw"
echo "Installed hamlog.pyw -> $INSTALL_DIR/hamlog.pyw"

# 4b. Install icon
ICON_SRC="$SCRIPT_DIR/assets/icon.png"
ICON_DIR="$HOME/.local/share/icons/hicolor/256x256/apps"
ICON_DST="$ICON_DIR/hamlog.png"
if [ -f "$ICON_SRC" ]; then
    mkdir -p "$ICON_DIR"
    cp "$ICON_SRC" "$ICON_DST"
    echo "Installed icon        -> $ICON_DST"
    if command -v gtk-update-icon-cache &>/dev/null; then
        gtk-update-icon-cache -f -t "$HOME/.local/share/icons/hicolor" 2>/dev/null || true
    fi
else
    echo "WARNING: assets/icon.png not found — skipping icon install"
fi

# 5. Create launcher script
cat > "$BIN_DIR/$APP_NAME" <<EOF
#!/usr/bin/env bash
exec python3 "\$HOME/.local/share/hamlog/hamlog.pyw" "\$@"
EOF
chmod +x "$BIN_DIR/$APP_NAME"
echo "Created launcher  -> $BIN_DIR/$APP_NAME"

# 6. Create .desktop file for application menu
cat > "$DESKTOP_DIR/hamlog.desktop" <<EOF
[Desktop Entry]
Name=POTA Logger
Comment=POTA Activator Hunter & Ham Radio Logger
Exec=$BIN_DIR/$APP_NAME
Terminal=false
Type=Application
Categories=HamRadio;Utility;
Icon=hamlog
EOF
echo "Created desktop entry -> $DESKTOP_DIR/hamlog.desktop"

# Refresh desktop database if the tool is available
if command -v update-desktop-database &>/dev/null; then
    update-desktop-database "$DESKTOP_DIR" 2>/dev/null || true
fi

# 7. Create log/config directory
mkdir -p "$HOME/HamLog"
echo "Created log directory  -> $HOME/HamLog"

# 8. Summary
echo ""
echo "=== Installation complete! ==="
echo ""
echo "To launch POTA Logger:"
echo "  hamlog"
echo ""

# Warn if ~/.local/bin is not on PATH
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo "NOTE: $BIN_DIR is not on your PATH."
    echo "      Add the following line to your ~/.bashrc or ~/.profile and restart your terminal:"
    echo ""
    echo "      export PATH=\"\$HOME/.local/bin:\$PATH\""
    echo ""
fi
