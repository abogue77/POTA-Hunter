# Getting Started

This page walks you through the first-launch setup so you can start hunting POTA activations.

---

## Step 1 — Open or Create a Logbook

On the first launch (or via **File → Open Logbook**), you will be prompted to select an ADIF file.

- To create a new logbook, type a new filename (e.g., `2026-activations.adi`) and click **Save**.
- To use an existing log, navigate to your `.adi` file and click **Open**.

POTA Hunter will remember the last-used logbook and reopen it automatically on the next launch.

---

## Step 2 — Enter Your Station Details

Open **Settings → Station Settings** and fill in:

| Field | Description |
|-------|-------------|
| **Callsign** | Your ham radio callsign (e.g., `N5EAB`) |
| **Grid Square** | Your Maidenhead grid locator, 4 or 6 characters (e.g., `EM10`) |

Click **Save**. Your callsign will appear in the header of the main window.

---

## Step 3 — Connect to Your Radio (Optional)

If you use **Flrig** for rig control, POTA Hunter can automatically read your current frequency and mode when you log a QSO, and tune your radio when you click a spot.

1. Make sure Flrig is running and connected to your radio.
2. Open **Settings → Flrig Settings**.
3. Verify the host (`127.0.0.1`) and port (`12345`) match your Flrig configuration.
4. Click **Test Connection** — you should see a success message with your current frequency.

If you don't use Flrig, you can still enter frequency and mode manually when logging.

See [Flrig Integration](Flrig-Integration) for more details.

---

## Step 4 — Set Up QRZ Lookups (Optional)

QRZ.com callsign lookups let you auto-fill operator name, location, and grid square when logging a QSO.

1. Open **Settings → QRZ Login**.
2. Enter your QRZ.com **username** and **password** (an XML subscription is required).
3. Click **Test Login** to verify.

See [QRZ Integration](QRZ-Integration) for details on subscription requirements.

---

## Step 5 — Download the Parks Database

The POTA parks database is required for grid square lookups and map markers. On first launch, POTA Hunter should prompt you to download it automatically.

To update it manually: **File → Update Parks DB**

This downloads `all_parks_ext.csv` from pota.app (~1 MB) and builds a local SQLite index at `~/POTA-Hunter/pota_parks.db`.

---

## Step 6 — Start Hunting

With setup complete:

1. The **POTA Spots** panel on the right will populate with live activations within a few seconds.
2. Click any row to pre-fill the log form and (if Flrig is connected) tune your radio.
3. Make the contact, then click **LOG QSO**.

That's it! See [Logging QSOs](Logging-QSOs) and [POTA Spots](POTA-Spots) for full details.
