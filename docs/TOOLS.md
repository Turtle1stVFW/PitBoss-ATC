# Tools

The zone editor (and its `/tester` page) live here. They write `atc/airports.json`
and read the ATC phrase / zone modules; the rest are maintainer scripts.

## Runway zone editor

Pilots reach it from **Setup → Draw zones on a map…**, from **Draw one…** beside
**Fires when** on Plan Flight, or by double-clicking `atc/Open-Zone-Editor.cmd`.
By hand it is:

```
py -3 tools/zone_server.py [--port 8777] [--airport nellis] [--page editor|tester]
```

The app launches it on `zone_editor_port` (8777 by default), reuses a copy already
listening rather than starting a second one, and stops it on exit.

A browser opens on satellite imagery of the field. Trace the areas the automatic
clearances watch, press **Save to airports.json**, done. Imagery is fetched by
the Python process and served from `http://127.0.0.1:<port>/tiles/…` — a
localhost page often cannot load Esri or OSM tiles itself, which is what a blank
black map usually means.

Because the map is georeferenced, every click already has a lat/lon. There is no
image to calibrate, no reference points to enter, and no DCS metres anywhere in
the workflow — `runway_position` converts lat/lon to feed metres at runtime.
On the map, lengths show in **feet** or **NM** (NM from about 0.1 NM up);
areas in ft² / acres / NM². Stored values stay metric (`radius_m`, etc.).

### Drawing

Set the **Trigger** and the **Runway** first, then use the tools down the left of
the map. What each tool is for:

| Shape | Use it for |
|-------|-----------|
| Polygon / rectangle | Areas: in position, hold short, parking, an agency area |
| Circle | The EOR or an agency area, where a radius is more natural than corners |
| Line | The runway centreline, **threshold first, far end last** |

Triggers and what the flow does with them:

| Trigger | Effect |
|---------|--------|
| `runway` | Centreline. Gives heading, length, and the fallback in-position box |
| `in_position` | Whole flight inside → cleared for takeoff |
| `eor` | Whole flight inside → monitor tower |
| `hold_short`, `parking` | Available to any step, nothing built in watches them |
| `delivery`, `ground`, `tower`, `departure`, `approach` | Agency areas: entering one is the cue to talk to that agency |
| `control_east`, `control_west`, `blackjack`, `joshua`, `center` | NTTR pass-on areas (Sally / Lee / range / R-2508). Entering one is the cue to talk to that agency |
| Custom name… | Anything you type. A step matches the name as a plain string, so a custom agency needs no code change |

Any of these can arm a step: on **Plan Flight** the step names the tag or one
specific area under **Fires when**. Only `in_position` and `eor` also have
built-in behaviour.

Draw one centreline per strip of concrete. 21R and 03L are the same runway used
in opposite directions, so drawing 21R fills in 03L reversed; reopening shows the
one line, not two stacked on each other.

An `in_position` or `eor` area with no runway set applies to *every* runway, which
is how you get cleared for a runway you are not lined up on. The editor warns on
save, but it is easier to just set the runway.

### Editing what you drew

Click a shape (on the map or in the list) and its corners light up:

| Action | How |
|--------|-----|
| Move the whole zone | Drag the blue ✚ handle, or drag the filled area / line itself |
| Move a corner | Drag a solid blue corner handle |
| Add a corner | Drag a faint midpoint handle between two corners |
| Delete a corner | Click a solid handle (polygons keep at least 3, lines at least 2) |
| Move / resize a circle | Drag ✚ (or the circle), or the edge handle to resize |
| Rotate | Drag the ↻ handle, or use **↺ 15°** / **15° ↻** in the sidebar |
| New zone | **New zone** or **N** — deselects so the next draw is a separate zone |
| Copy / paste | **Copy** or Ctrl+C, then **Paste as new** or Ctrl+V — always a new one-shape zone, nudged off the original |
| Show / hide on map | Eye button on each row (not saved). **H** toggles the selected zone; **Show all** / **Hide all** for the list |
| Delete one zone | **Delete zone**, the × on its row, or Delete/Backspace — never clears the whole map |

There is no map “Clear All”. Leaflet.Draw’s trash toolbar was removed for that reason.

Each zone is exactly one shape (one polygon, circle or line). A MultiPolygon
import is split into separate zones.

Sidebar fields (trigger, runway, label, altitude band) apply to the **selected**
zone as you type — click the zone in the list first. That only updates the
editor. **Lock zone** protects a shape from accidental reshape or delete
(Unlock… asks first). Shipped Nellis defaults start locked. Press
**Save to airports.json** to write the file (do this before adding
more zones if you want a checkpoint, and always before closing). Click bare map,
or **Finish editing this zone**, to go back to setting up a new one.

### Altitude bands

A drawn area is otherwise infinitely tall, so traffic overhead sits inside your
ramp. **Altitude band** is optional, in feet AGL:

| Area | Band |
|------|------|
| Ramp, EOR, hold short, in position | Leave both blank — a jet on the ground is the only thing that can be settled there anyway |
| Tower area | `0` to about `5000`, so the overhead pattern counts and airliners at altitude do not |
| An overhead break only | `1500` to `5000` |

A floor of `0` means the surface and includes a jet reading slightly *below* it:
field elevation is one number for a field that is not flat, and a parked aircraft
routinely comes back around −20 ft AGL. A floor above zero is enforced exactly as
written. Live tracks show AGL in their tooltip, which is the number to read off
while setting a band.

### Checking it against DCS

Tick **Show live CAOC tracks** with a mission running. Your jet is the red one,
and the sidebar says which zone it is in. Taxi onto the runway and the verdict
should flip to the in-position area on its own.

DCS terrain is a projection of the real world rather than a copy of it, so if
every track sits at the same small offset from where it should be, trust the feed
and move the shapes — the shapes are what the flow compares against.

### The `calibrated` flag

Set automatically, and only true with both a centreline and an `in_position`
area. The flow app's two built-in triggers fire nothing at an uncalibrated field:
estimated geometry can be a hundred metres out, which is the difference between
lined up and still on the taxiway. The Fly tab says so when that is why nothing
is happening.

A step pointed at an area you drew does not wait for the flag — drawing and
checking the area against live tracks *is* the calibration for that area. That is
also why the shipped Nellis field is uncalibrated but still has a usable `tower`
area: it is a 5 NM circle derived from the field reference point, not a traced
guess at where the concrete is.

## Reference overlays (NTTR, airspace packs)

Large KML/KMZ packs (MOAs, restricted areas, targets, MTRs, etc.) can sit on the
map as a **view-only** reference while you draw trigger zones. They never become
zones and are never written to `airports.json`.

1. Copy the file into `tools/overlays/` (e.g. `NTTR.kmz`), or use **Upload KMZ /
   KML** in the editor sidebar.
2. Open the editor, pick the pack under **Reference overlay**, click **Show on
   map**.
3. Use the **Layers** panel on the map (top-right, under the basemap control):
   nested folders with checkboxes (Airspace / targets on by default; Navigation
   / MTRs off). Expand branches, toggle parents or leaves, and search for a
   named point / MOA to zoom to it.

The first load builds a `.kmz.geojson` cache next to the source so the next open
is faster. Packs and caches under `tools/overlays/` are gitignored — keep your
own copy locally.

## NTTR agency airspace (Sally / Lee)

Shipped Nellis zones include NATCF / range polygons so Blackjack can pass a
recovery to **Sally** (Control East, IFG ch 7) or **Lee** (Control West, ch 8)
from position. FAA AIS publishes Desert MOA, Reveille, and the R-4806/07/08/09
restricted areas — not the corridor names. Lee Corridor is sketched from
NELLISAFBI 11-250 (south of the NAFR between Nellis and R-4808S / R-4807).

`tools/agency_airspace.kml` is the pass-on overlay (Google Earth, or copy into
`tools/overlays/` for the zone editor). Rebuild it, or refresh the polygons, with:

```
py -3 tools/export_agency_airspace.py
py -3 tools/import_nttr_agencies.py           # cached GeoJSON if present, else fetch
py -3 tools/import_nttr_agencies.py --fetch   # re-query FAA AIS
```

Not a substitute for the IFG chart.

## Google Earth instead

Draw polygons and paths over the field in Google Earth Pro, save the folder as
KML or KMZ, then either drop the file on **Import GeoJSON / KML** in the editor
(this **imports as editable zones**), or go straight in:

```
py -3 tools/import_zones.py nellis KLSV-zones.kml --dry-run
py -3 tools/import_zones.py nellis KLSV-zones.kml
```

Google Earth has no property editor, so the placemark *name* carries the tags.
Both of these read the same:

```
21R in position
in_position:21R
eor:21R:NW EOR          <- trigger:runway:label
Runway 21R
hold short 21R
```

A bare number only counts as a runway if it has a side letter or the name says
"runway", so `Row 18 parking` stays a parking area. Anything untagged is listed
and skipped rather than guessed at, and a runway the field does not have is
called out by name.

Circles do not exist in KML. Use a polygon, or a point with a `radius_m` in its
ExtendedData. `trigger`, `runway`, `min_alt_ft` and `max_alt_ft` are read from
ExtendedData too, and win over anything inferred from the name:

```xml
<ExtendedData>
  <Data name="trigger"><value>tower</value></Data>
  <Data name="min_alt_ft"><value>0</value></Data>
  <Data name="max_alt_ft"><value>5000</value></Data>
</ExtendedData>
```

## Checks

```
py -3 tools/check_zone_geo.py      # parsing, tag inference, reciprocals, writing
py -3 tools/check_zone_server.py   # HTTP surface, on a spare port and a scratch file
py -3 atc/check_route_tester.py    # offline tester evaluation (no HTTP)
```

`check_zone_server.py` starts the server itself, so nothing needs to be running.
Both write only to temporary files; the last thing they assert is that the real
`airports.json` was never touched. `/api/tracks` reports no feed if Opus is not
up, which is not a failure.

Saving from the editor keeps a one-shot `airports.json.bak` next to the file.

## Map jet (Fly test mode)

Same process as the zone editor; `/tester` is the map Fly uses as your aircraft.
From the app: **Setup → Map is my jet…**. Standalone:

```
py -3 tools/zone_server.py --page tester
```

or `atc/Open-Route-Tester.cmd`. Plot a filed route, drag or Play the red jet.
**Drive Fly ownship** (on by default) feeds that position into Fly so auto
clearances, voice, and TTS run for real. CAOC still supplies tankers and other
tracks. Weather and radio stay on the Fly tab.

Drop `NTTR.kml` in `tools/overlays/` so DREAM / JUNNO / FYTTR label at CAOC
coordinates. A bundled extract lives in `atc/nttr_navpoints.json`.

```
py -3 atc/check_route_tester.py   # synthetic positions, no HTTP
```

## Vendored libraries

`vendor/` holds Leaflet 1.9.4 and Leaflet.draw 1.0.4, so the editor works offline
and pulls no third-party code at runtime. Satellite tiles come from Esri World
Imagery, which needs no key; the layer control also offers OpenStreetMap.

`vendor/patch_leaflet_draw.py` declares the `type` variable that Leaflet.draw
1.0.4 assigns without declaring in `readableArea`, which throws under strict mode
on every mouse move while drawing ([Leaflet.draw#899][ldraw]). It has already
been applied; it is idempotent, so re-run it after replacing the vendored file.

[ldraw]: https://github.com/Leaflet/Leaflet.draw/issues/899
