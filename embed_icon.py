"""
Embed an ICO into a PyInstaller exe using rcedit, preserving the PyInstaller payload.

rcedit uses Windows UpdateResource which rebuilds the PE resource section and
truncates the file — discarding PyInstaller's appended archive. This script:
  1. Finds and saves the PyInstaller payload from the original exe
  2. Runs rcedit to update the icon resources
  3. Re-appends the payload so the exe still runs
"""

import subprocess, pefile, sys, os

ROOT   = os.path.dirname(os.path.abspath(__file__))
EXE    = os.path.join(ROOT, "dist", "POTA-Logger.exe")
ICO    = os.path.join(ROOT, "assets", "icon.ico")
RCEDIT = os.path.join(ROOT, "rcedit-x64.exe")

print("Reading original exe...")
with open(EXE, "rb") as f:
    original = f.read()

# Find where the PyInstaller payload begins (right after the last PE section)
pe = pefile.PE(EXE, fast_load=True)
payload_start = max(
    s.PointerToRawData + s.SizeOfRawData
    for s in pe.sections
)
pe.close()
payload = original[payload_start:]
print(f"  PE ends at offset {payload_start:,} — payload is {len(payload):,} bytes")

if len(payload) < 1024:
    print("ERROR: payload too small — exe may already be corrupted.")
    sys.exit(1)

# Run rcedit (this strips the payload but correctly updates icon resources)
print("Running rcedit...")
result = subprocess.run([RCEDIT, EXE, "--set-icon", ICO])
if result.returncode != 0:
    print("ERROR: rcedit failed.")
    sys.exit(1)

# Re-append the PyInstaller payload
print("Re-appending PyInstaller payload...")
with open(EXE, "ab") as f:
    f.write(payload)

final_size = os.path.getsize(EXE)
print(f"  Final exe size: {final_size:,} bytes")
print("Done — icon embedded, exe intact.")
