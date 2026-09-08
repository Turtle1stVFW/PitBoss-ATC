# ATC radio export (frequency gate)

Install is automated — you should not edit `Export.lua` by hand.

## From the app

Setup → Controls → **Install DCS radio export…**

That copies `ATC-RadioExport.lua` into each `Saved Games\DCS*\Scripts\` profile and
appends a marked hook to `Export.lua` without touching the SRS line.

## From CLI / Windows installer

```bat
atc\Install-DCS-Radio-Export.cmd
atc\Install-DCS-Radio-Export.cmd --status
```

Or:

```bat
py -3 atc\install_dcs_radio_export.py
```

For a packaged installer, run the same command as a post-install custom action
(optionally pass `--source "C:\Path\To\staged\ATC-RadioExport.lua"`). See
[`atc/installer/README.md`](../atc/installer/README.md).

## What it writes

While you are in a unit, DCS writes:

`Saved Games\DCS*\ATC-ExternalAudio\radios.json`

## Performance

This is **not** a heavy export like Tacview / full SRS:

- Almost every frame: one timer check, then return
- About **1 Hz** in the cockpit: read a small cached list of radio device ids
- Disk write only when frequency/unit **changes**
- Menus / no unit: keepalive write at most every 5 s

Re-run **Install DCS radio export…** after updating the script so Saved Games gets the new file.
