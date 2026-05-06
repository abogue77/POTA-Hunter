# ADIF & Logbooks

POTA Hunter stores all QSOs in **ADIF (Amateur Data Interchange Format)** files with the `.adi` extension. ADIF is the standard exchange format accepted by POTA, LOTW, eQSL, and most logging programs.

---

## Creating a New Logbook

Go to **File → Open Logbook**, type a new filename (e.g., `pota-2026.adi`), and click **Save**. POTA Hunter creates the file with an ADIF header and begins logging to it immediately.

There is no limit on the number of logbook files you can create. You might keep separate files per year, per park, or per trip.

---

## Opening an Existing Logbook

Go to **File → Open Logbook** and navigate to an existing `.adi` file. POTA Hunter loads all existing QSOs into the session table and appends new ones.

The last-opened logbook is remembered and reopened automatically on the next launch.

---

## Importing an ADIF File

**File → Import ADIF** appends records from another ADIF file into the currently open logbook. Use this to merge logs from another program or from a previous activation.

Duplicate QSOs are not automatically detected — review the log after importing if needed.

---

## Exporting an ADIF File

**File → Export ADIF** saves a copy of the current log to a location you specify. The exported file is a valid ADIF 3.1.0 file ready to upload to:

- [POTA](https://parksontheair.com) — upload to your account under "Activate a Park"
- [Logbook of the World (LOTW)](https://lotw.arrl.org)
- [eQSL](https://eqsl.cc)
- [HAMRS](https://hamrs.app), [Log4OM](https://www.log4om.com/), or any other logging software

---

## ADIF File Format

Each QSO record in the `.adi` file looks like this:

```
<CALL:5>N5EAB <QSO_DATE:8>20260501 <TIME_ON:4>1430 <FREQ:6>14.265 
<BAND:3>20m <MODE:3>SSB <RST_SENT:2>59 <RST_RCVD:2>59 
<GRIDSQUARE:4>EM10 <POTA_REF:6>K-0001 <COMMENT:8>CQ POTA <EOR>
```

ADIF is plain text — you can open `.adi` files in any text editor.

---

## Uploading to POTA

1. Export your log: **File → Export ADIF**
2. Log in to [parksontheair.com](https://parksontheair.com)
3. Go to **My Log → Upload Log**
4. Upload the `.adi` file

POTA will credit the QSOs to the park references in the `POTA_REF` field.

---

## Backup

Your logbook files are stored in `~/POTA-Hunter/` alongside `config.json` and the parks database. Back up this entire folder periodically to avoid losing your logs. Cloud storage (OneDrive, Dropbox, Google Drive) works well for keeping an offsite copy.
