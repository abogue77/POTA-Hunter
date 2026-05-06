# Configuration

All settings are stored in `~/POTA-Hunter/config.json` and can be changed through the **Settings** menus in the app. You can also edit the file directly in a text editor while the app is closed.

---

## Settings Menus

| Menu Item | Opens |
|-----------|-------|
| Settings → Station Settings | Callsign and grid square |
| Settings → QRZ Login | QRZ.com username and password |
| Settings → Flrig Settings | Rig control host and port |

Theme, scan interval, filters, and other options are controlled from the main window UI (buttons and dropdowns).

---

## config.json Reference

Below is the complete structure of `config.json` with all default values.

```json
{
  "callsign": "",
  "gridsquare": "",
  "qrz_user": "",
  "qrz_pass": "",
  "flrig_host": "127.0.0.1",
  "flrig_port": 12345,
  "last_logbook": "",
  "theme": "dark",
  "pota_band": "All",
  "pota_mode": "All",
  "pota_hide_qrt": false,
  "pota_itu_r1": true,
  "pota_itu_r2": true,
  "pota_itu_r3": true,
  "pota_respot_enabled": false,
  "pota_scan_skip_worked": false,
  "pota_scan_interval": 15
}
```

---

## Field Descriptions

### Station

| Key | Type | Description |
|-----|------|-------------|
| `callsign` | string | Your ham radio callsign. Appears in the header and is saved to ADIF as `MY_CALL`. |
| `gridsquare` | string | Your Maidenhead grid locator (4 or 6 characters). Saved as `MY_GRIDSQUARE` in ADIF. |

### QRZ

| Key | Type | Description |
|-----|------|-------------|
| `qrz_user` | string | QRZ.com username for callsign lookups. Requires an XML subscription. |
| `qrz_pass` | string | QRZ.com password. Stored in plain text. |

### Flrig

| Key | Type | Description |
|-----|------|-------------|
| `flrig_host` | string | Hostname or IP of the Flrig XML-RPC server. Default: `127.0.0.1`. |
| `flrig_port` | integer | Port number. Default: `12345`. |

### Logbook

| Key | Type | Description |
|-----|------|-------------|
| `last_logbook` | string | Full path to the last-opened `.adi` file. Reopened automatically on launch. |

### Appearance

| Key | Type | Values |
|-----|------|--------|
| `theme` | string | `"dark"` or `"light"` |

### POTA Spot Filters

| Key | Type | Description |
|-----|------|-------------|
| `pota_band` | string | Band filter: `"All"`, `"40m"`, `"20m"`, etc. |
| `pota_mode` | string | Mode filter: `"All"`, `"SSB"`, `"CW"`, `"FT8"`, etc. |
| `pota_hide_qrt` | boolean | Hide spots with `QRT` in comments. |
| `pota_itu_r1` | boolean | Include ITU Region 1 (Europe, Africa, Middle East). |
| `pota_itu_r2` | boolean | Include ITU Region 2 (Americas). |
| `pota_itu_r3` | boolean | Include ITU Region 3 (Asia, Pacific). |

### Auto-Scan

| Key | Type | Description |
|-----|------|-------------|
| `pota_respot_enabled` | boolean | Show the Activator Mode / Re-Spot panel. |
| `pota_scan_skip_worked` | boolean | Skip already-worked callsigns during auto-scan cycling. |
| `pota_scan_interval` | integer | Seconds between automatic spot fetches. Default: `15`. |

---

## Resetting to Defaults

Close the app, delete `~/POTA-Hunter/config.json`, and relaunch. The file will be recreated with all default values. Your logbook files are not affected.
