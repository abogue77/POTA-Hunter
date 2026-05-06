# POTA Spots

The **POTA Spots** panel shows a live table of active parks-on-the-air activations, updated automatically from the POTA API.

---

## Spot Table Columns

| Column | Description |
|--------|-------------|
| **Activator** | Callsign of the operator at the park |
| **Park** | POTA reference number (e.g., `K-0001`) |
| **Park Name** | Full park name from the parks database |
| **Freq** | Frequency in kHz |
| **Mode** | Transmission mode (SSB, CW, FT8, etc.) |
| **Spotted** | How long ago the spot was posted |
| **Comments** | Spotter comment (may include signal reports or `QRT`) |

Click any column header to sort by that column.

---

## Selecting a Spot

Click a row to:
- Pre-fill the **Log QSO** form (callsign, park, grid square)
- Auto-tune your radio via Flrig to the spot's frequency
- Show the activator's park location on the map with a blue highlight
- Display station details in the **Tuned Station** card

---

## Color Coding

| Row color | Meaning |
|-----------|---------|
| Normal | Unworked activator |
| Highlighted Green| Activator already logged in this session |
| Highlighted Blue| Radio (Flrig) tuned to activator |


---

## Filters

Use the controls above the spot table to narrow the list.

### Band Filter
Select a specific band (160m, 80m, 40m, 20m, etc.) or **All** to show every band. The dropdown is populated automatically from the currently active spots.

### Mode Filter
Select a specific mode (SSB, CW, FT8, FM, etc.) or **All**. Also auto-populated from live spots.

### Hide QRT
Check this box to hide spots whose comment contains `QRT` (the operator has left the air). Useful to declutter the list.

### ITU Region Filters
Check or uncheck **R1** (Europe/Africa), **R2** (Americas), and **R3** (Asia/Pacific) to show only spots from the desired region. Regions are determined by the park's country.

---

## Auto-Scan

POTA Hunter refreshes the spot list on a configurable interval.

| Control | Description |
|---------|-------------|
| **Scan** button (header) | Pause / resume fetching |
| **Interval slider** | Time between fetches in seconds (default: 15) |
| **Skip Worked** checkbox | When auto-scanning, skip activators already in the log |

When a new spot appears for a station you haven't logged, it is highlighted briefly.

---

## Spot Age

The **Spotted** column shows the age of the spot. Spots typically stay active for 15–30 minutes on the POTA API before aging out. Very old spots (>30 min) may indicate the activator is still active or may have left without posting QRT.

---

## Manual Refresh

Click the **Scan** button to pause and click it again to immediately trigger a fresh fetch, or simply wait for the next automatic update.
