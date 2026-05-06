# QRZ Integration

POTA Hunter can look up callsigns on [QRZ.com](https://www.qrz.com) to automatically fill in operator name, location, and grid square when logging a QSO.

---

## Requirements

QRZ callsign lookups via the XML API require a **QRZ.com XML subscription** (paid). Basic free accounts do not have API access.

If you don't have a QRZ subscription, you can skip this setup — POTA Hunter works fine without it.

---

## Setup

1. Open **Settings → QRZ Login**.
2. Enter your QRZ.com **username** and **password**.
3. Click **Test Login** — POTA Hunter will attempt to authenticate and report success or an error message.
4. Click **Save**.

Credentials are stored in `~/POTA-Hunter/config.json`.

---

## How Lookups Work

When you type or select a callsign in the log form:

1. POTA Hunter authenticates with QRZ using your credentials to obtain a session key.
2. It queries the callsign and receives an XML response.
3. The operator's name, QTH (city/state/country), and grid square are extracted and populated in the form.

The session key is cached to avoid re-authenticating on every lookup.

---

## Data Retrieved

| Field | Description |
|-------|-------------|
| **First / Last Name** | Operator's name from QRZ profile |
| **City, State, Country** | Address fields |
| **Grid Square** | Maidenhead locator from QRZ profile |
| **Email** | Available in profile (not logged) |

---

## Privacy Note

Your QRZ username and password are stored in plain text in `~/POTA-Hunter/config.json`. Ensure this file is not shared or committed to version control.

---

## Troubleshooting

| Symptom | Likely Cause |
|---------|-------------|
| "Login failed" | Wrong username/password, or QRZ server is down |
| "No XML subscription" | Your QRZ account doesn't have API access |
| Lookups return no data | Callsign not in QRZ database (common for non-US calls) |
| Session expired errors | POTA Hunter will automatically re-authenticate |
