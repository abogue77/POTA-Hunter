# FAQ & Troubleshooting

---

## General

**Q: Does POTA Hunter work without an internet connection?**

A: The spot table and map markers require an internet connection to fetch live data from the POTA API. However, you can still log QSOs manually if you're offline — the logbook and parks database are stored locally.

---

**Q: Can I use POTA Hunter without a radio connected?**

A: Yes. Flrig integration is optional. Without it, you enter frequency and mode manually in the log form. The rest of the app (spots, map, logging) works normally.

---

**Q: Where are my log files stored?**

A: In `~/POTA-Hunter/` — that's `C:\Users\<YourName>\POTA-Hunter\` on Windows, or `/home/<yourname>/POTA-Hunter/` on Linux/macOS.

---

**Q: Can I have multiple logbooks?**

A: Yes. Each `.adi` file is an independent logbook. Switch between them with **File → Open Logbook**. Only one logbook is active at a time.

---

## POTA Spots

**Q: The spot list is empty — why?**

A: A few possibilities:
- No parks are currently active (try a different time of day or weekend)
- The POTA API may be temporarily down — check [pota.app](https://pota.app) in a browser
- Your ITU Region filters may be set to exclude all regions — check the R1/R2/R3 checkboxes
- The band or mode filter is set to something with no active spots — try setting both to **All**

---

**Q: Spots show but the map has no markers.**

A: The map needs the parks database to get lat/lon coordinates. Go to **File → Update Parks DB** and wait for the download to complete.

---

**Q: A spot shows a park I don't recognize.**

A: New parks are added to POTA regularly. Update your local parks database (**File → Update Parks DB**) to get the latest list including the park name and location.

---

**Q: The "Spotted" time seems wrong.**

A: POTA Hunter displays spot age based on the timestamp in the POTA API response. If your system clock is significantly off from UTC, times may appear shifted. Make sure your computer's clock is synchronized.

---

## Logging

**Q: Frequency is blank when I log a QSO.**

A: Either Flrig is not connected, or you need to enter the frequency manually. If Flrig is running, check **Settings → Flrig Settings** and click **Test Connection** to diagnose.

---

**Q: I accidentally logged a duplicate QSO — how do I remove it?**

A: Double-click the QSO in the log table to open the Edit QSO dialog, then click **Delete**.

---

**Q: My log isn't showing up on POTA after uploading.**

A: Make sure your `.adi` file contains the `POTA_REF` field (filled in via the Park # field when logging). Also confirm the `MY_CALL` field matches your POTA account callsign. You can open the `.adi` in a text editor to verify.

---

## Flrig

**Q: "Test Connection" fails but Flrig is running.**

A: Confirm the host and port in **Settings → Flrig Settings** match Flrig's XML-RPC server settings (check Flrig under Configure → Setup). Also verify no firewall is blocking localhost port 12345.

---

**Q: Auto-tune doesn't move my radio when I click a spot.**

A: Check the Flrig connection with **Test Connection**. If it succeeds, try clicking a spot again. Note that very large frequency jumps (e.g., HF to VHF) may be rejected by some radios — the rig's internal limits apply.

---

## QRZ

**Q: Callsign lookups return "No XML subscription."**

A: The QRZ XML API requires a paid XML data subscription. Basic free accounts cannot use the API. See [QRZ.com subscriptions](https://www.qrz.com/i/subscriptions.html) for options.

---

**Q: Lookups work sometimes but not others.**

A: The QRZ session key expires after a period of inactivity. POTA Hunter should automatically re-authenticate. If lookups are consistently failing, try saving your credentials again in **Settings → QRZ Login**.

---

## Map

**Q: The map opens but shows a blank page.**

A: Make sure your browser allows connections to `127.0.0.1` (localhost). Some browsers or privacy extensions block local connections. Try opening the map URL shown in the app directly in your browser.

---

**Q: Can I use a different map provider?**

A: The current version uses a built-in map renderer. Third-party tile providers are not configurable in the current release.

---

## Windows

**Q: Windows Defender blocks the EXE.**

A: As a PyInstaller-built executable from an individual developer, the EXE may trigger SmartScreen on first run. Click **More info → Run anyway** to proceed. You can also run from source (`python potahunter.pyw`) if you prefer to avoid this entirely.

---

**Q: The app window doesn't scale properly on a high-DPI display.**

A: Right-click `POTA-Logger.exe` → Properties → Compatibility → Change high DPI settings → check "Override high DPI scaling behavior" and set to "Application."
