Drop KML/KMZ reference packs here (e.g. NTTR.kml).
They appear under Reference overlay in the zone editor and the route tester.
A .geojson cache is built beside each file on first load.

Named Point placemarks (DREAM, JUNNO, FYTTR, …) are also read into the tester
fix catalog so filed routes plot at the NTTR coordinates, not the FAA SID
fallbacks in atc/fixes.json.

These are view-only — they are not written into airports.json.
