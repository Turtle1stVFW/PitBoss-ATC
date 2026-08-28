/*
 * Offline route tester. Internal tool — see tools/README.md.
 *
 * Same localhost tile proxy as the zone editor. Ownship is synthetic: drag or
 * play along a filed route; ticks go to /api/tester/tick (no DCS, no SRS).
 */
"use strict";

const COLOURS = {
  runway: "#f59e0b",
  in_position: "#22c55e",
  eor: "#3b82f6",
  hold_short: "#ef4444",
  parking: "#a855f7",
  delivery: "#14b8a6",
  ground: "#84cc16",
  tower: "#e879f9",
  departure: "#38bdf8",
  approach: "#818cf8",
  blackjack: "#f97316",
  joshua: "#ef4444",
  center: "#a78bfa",
  control_east: "#22d3ee",
  control_west: "#2dd4bf",
  other: "#94a3b8",
};

const el = (id) => document.getElementById(id);
const colourFor = (trigger) => COLOURS[trigger] || COLOURS.other;

const state = {
  airport: "",
  playing: false,
  waypoints: [],
  playIdx: 0,
  playT: 0,
  lastTickMs: 0,
  lastSendMs: 0,
  heading: 210,
  lat: 36.236,
  lon: -115.034,
  runwayHdg: null,
  fpAltitude: "",
  lastPhrase: "",
  lastChannel: "",
  opusFlights: [],
  fixes: [],
  traffic: [],
  trafficSeq: 1,
  selectedTraffic: "",
};

const map = L.map("map", { zoomControl: true, maxZoom: 20 });
map.createPane("refOverlay");
map.getPane("refOverlay").style.zIndex = 350;
map.createPane("zones");
map.getPane("zones").style.zIndex = 400;
map.createPane("routePane");
map.getPane("routePane").style.zIndex = 450;

const satellite = L.tileLayer("/tiles/esri/{z}/{y}/{x}", {
  maxZoom: 20,
  maxNativeZoom: 19,
  attribution: "Imagery © Esri, Maxar, Earthstar Geographics",
}).addTo(map);
const labels = L.tileLayer("/tiles/esri-labels/{z}/{y}/{x}", {
  maxZoom: 20,
  maxNativeZoom: 19,
  opacity: 0.85,
});
const streets = L.tileLayer("/tiles/osm/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "© OpenStreetMap contributors",
});
L.control
  .layers({ Satellite: satellite, Streets: streets }, { "Place names": labels })
  .addTo(map);
L.control.scale({ imperial: true, metric: false }).addTo(map);

const zoneLayer = L.geoJSON(null, {
  pane: "zones",
  style: (feat) => {
    const trig = (feat.properties && feat.properties.trigger) || "other";
    return {
      color: colourFor(trig),
      weight: trig === "runway" ? 3 : 1.5,
      fillOpacity: trig === "runway" ? 0 : 0.12,
      opacity: 0.85,
    };
  },
  onEachFeature: (feat, layer) => {
    const p = feat.properties || {};
    const label = [p.trigger, p.name || p.id].filter(Boolean).join(" · ");
    if (label) layer.bindTooltip(label);
  },
}).addTo(map);

const routeLine = L.polyline([], {
  pane: "routePane",
  color: "#fbbf24",
  weight: 3,
  opacity: 0.95,
}).addTo(map);
const routeDots = L.layerGroup().addTo(map);
const fixLayer = L.layerGroup().addTo(map);
const trackLayer = L.layerGroup().addTo(map);
const trafficLayer = L.layerGroup().addTo(map);

function planeIcon(color, heading, selected) {
  const rot = Number(heading) || 0;
  const html =
    `<svg viewBox="0 0 32 32" style="transform:rotate(${rot}deg)">` +
    `<path fill="${color}" stroke="#0b0f14" stroke-width="1.1" ` +
    `d="M16 2.5 L18.4 12.2 L28 16.2 L18.6 15.4 L17.8 24.2 L21.5 28.2 L16 26.2 ` +
    `L10.5 28.2 L14.2 24.2 L13.4 15.4 L4 16.2 L13.6 12.2 Z"/></svg>`;
  return L.divIcon({
    className: "plane-icon" + (selected ? " selected" : ""),
    html,
    iconSize: [28, 28],
    iconAnchor: [14, 14],
  });
}

const jet = L.marker([state.lat, state.lon], {
  icon: planeIcon("#ef4444", 210, false),
  draggable: true,
  zIndexOffset: 1000,
}).addTo(map);

function setJetHeading(deg) {
  let h = Number(deg);
  if (!Number.isFinite(h)) h = 0;
  h = ((h % 360) + 360) % 360;
  state.heading = h;
  jet.setIcon(planeIcon("#ef4444", h, false));
  if (el("hdg") && el("hdg") !== document.activeElement) {
    el("hdg").value = String(Math.round(h));
  }
  if (el("hdgVal")) el("hdgVal").textContent = `${Math.round(h)}°`;
}

function haversineNm(a, b) {
  const rlat1 = (a[0] * Math.PI) / 180;
  const rlat2 = (b[0] * Math.PI) / 180;
  const dlat = ((b[0] - a[0]) * Math.PI) / 180;
  const dlon = ((b[1] - a[1]) * Math.PI) / 180;
  const h =
    Math.sin(dlat / 2) ** 2 +
    Math.cos(rlat1) * Math.cos(rlat2) * Math.sin(dlon / 2) ** 2;
  return 2 * Math.atan2(Math.sqrt(h), Math.sqrt(1 - h)) * 3440.065;
}

function bearingDeg(a, b) {
  const phi1 = (a[0] * Math.PI) / 180;
  const phi2 = (b[0] * Math.PI) / 180;
  const dlon = ((b[1] - a[1]) * Math.PI) / 180;
  const y = Math.sin(dlon) * Math.cos(phi2);
  const x =
    Math.cos(phi1) * Math.sin(phi2) - Math.sin(phi1) * Math.cos(phi2) * Math.cos(dlon);
  return ((Math.atan2(y, x) * 180) / Math.PI + 360) % 360;
}

function lerp(a, b, t) {
  return a + (b - a) * t;
}

function qsAirport() {
  return new URLSearchParams(location.search).get("airport") || "";
}

function syncSliders() {
  el("altVal").textContent = `${el("alt").value} ft AGL`;
  el("spdVal").textContent = `${el("spd").value} kt`;
  if (el("hdgVal") && el("hdg")) {
    el("hdgVal").textContent = `${el("hdg").value}°`;
  }
  if (el("trafficAltVal")) {
    el("trafficAltVal").textContent = `${el("trafficAlt").value} ft AGL`;
  }
}

function renderHud(out) {
  el("hudOwner").textContent = `Owner — ${out.owner_spoken || out.owner || "none"}`;
  el("hudSector").textContent = `NATCF — ${out.sector_spoken || out.sector || "—"}`;
  const jn =
    out.joshua_nm != null ? ` · Joshua ${out.joshua_nm} NM` : "";
  el("hudHandoff").textContent = `Next — ${out.next_handoff_spoken || out.next_handoff || "—"} (${out.field_nm != null ? out.field_nm + " NM field" : "—"}${jn})`;
  const zones = (out.inside || [])
    .map((z) => z.label || z.trigger)
    .filter(Boolean);
  el("hudZones").textContent = `Inside — ${zones.join(", ") || "none"}`;
  const armed = (out.armed || []).filter((s) => s.armed).map((s) => s.label);
  const waiting = (out.armed || [])
    .filter((s) => s.held && !s.armed)
    .map((s) => `${s.label} (${s.waiting})`);
  el("hudArmed").textContent = `Armed — ${armed.join(", ") || waiting.join(", ") || "none"}`;
  if (out.hop) el("hop").textContent = out.hop;
  if (out.runway_hdg != null) state.runwayHdg = out.runway_hdg;
    el("driveStatus").textContent = out.driving_fly
      ? "Fly is using this jet and this filed route. Talk on the Fly tab."
      : "Preview only — tick Drive Fly ownship to feed the app.";
  if (el("overrideWx").checked && out.weather) {
    const w = out.weather;
    const dir = w.wind_dir == null ? "—" : String(w.wind_dir).padStart(3, "0");
    const spd = w.wind_speed_kt == null ? "—" : w.wind_speed_kt;
    const alt = w.altimeter_inhg == null ? "—" : Number(w.altimeter_inhg).toFixed(2);
    const clock = out.mission_hhmm || el("missionClock").value || "—";
    el("wxHint").textContent = `Fly using tester weather · ${dir}/${spd} A${alt} · ${clock}L`;
  } else if (!el("overrideWx").checked) {
    el("wxHint").textContent =
      "Checked: Fly uses these winds, altimeter, ceiling, and clock instead of Opus. Night 2200–0800L calm-wind departures are 03s. Uncheck to go back to Opus.";
  }
}

function tickBody(extra) {
  const ll = jet.getLatLng();
  state.lat = ll.lat;
  state.lon = ll.lng;
  return Object.assign(
    {
      airport: state.airport,
      lat: ll.lat,
      lon: ll.lng,
      alt_ft_agl: Number(el("alt").value),
      heading_deg: state.heading,
      speed_kt: Number(el("spd").value),
      route: el("route").value,
      callsign: (el("callsign").value || "").trim(),
      runway: "",
      fp_altitude: state.fpAltitude || "",
      drive_fly: el("driveFly").checked,
      override_weather: el("overrideWx").checked,
      metar: el("metar").value,
      wind_dir: Number(el("windDir").value),
      wind_speed_kt: Number(el("windKt").value),
      altimeter_inhg: Number(el("altimeter").value),
      visibility_sm: Number(el("visSm").value),
      ceiling_ft: el("ceilingFt").value === "" ? "" : Number(el("ceilingFt").value),
      mission_hhmm: el("missionClock").value,
      traffic: trafficPayload(),
    },
    extra || {}
  );
}

async function sendTick() {
  try {
    const res = await fetch("/api/tester/tick", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(tickBody()),
    });
    const out = await res.json();
    if (!out.ok) {
      el("driveStatus").textContent = out.error || "tick failed";
      return;
    }
    renderHud(out);
  } catch (err) {
    el("driveStatus").textContent = String(err);
  }
}

async function pollTracks() {
  if (!el("showTracks").checked) {
    trackLayer.clearLayers();
    return;
  }
  try {
    const res = await fetch(`/api/tracks?airport=${encodeURIComponent(state.airport)}`);
    const out = await res.json();
    trackLayer.clearLayers();
    if (!out.ok) return;
    (out.tracks || []).forEach((t) => {
      const id = String(t.id || "");
      if (t.own || id.startsWith("map-")) return;
      const colour =
        t.coalition === "red" ? "#ef4444" : t.coalition === "blue" ? "#3b82f6" : "#e2e8f0";
      L.marker([t.lat, t.lon], {
        icon: planeIcon(colour, t.heading || 0, false),
        pane: "routePane",
        interactive: false,
        keyboard: false,
      })
        .bindTooltip(
          [t.label, t.agl_ft != null ? `${Math.round(t.agl_ft)} ft AGL` : ""]
            .filter(Boolean)
            .join(" · ")
        )
        .addTo(trackLayer);
    });
  } catch (_) {
    /* CAOC optional */
  }
}

function drawRoute(plotted) {
  const wps = plotted.waypoints || [];
  state.waypoints = wps;
  const latlngs = wps.map((w) => [w.lat, w.lon]);
  routeLine.setLatLngs(latlngs);
  routeDots.clearLayers();
  wps.forEach((w) => {
    L.circleMarker([w.lat, w.lon], {
      pane: "routePane",
      radius: 5,
      color: "#fbbf24",
      fillColor: "#0b0f14",
      fillOpacity: 1,
      weight: 2,
    })
      .bindTooltip(`${w.id}${w.say && w.say !== w.id ? " · " + w.say : ""}`)
      .addTo(routeDots);
  });
  const unknown = plotted.unknown || [];
  el("routeUnknown").textContent = unknown.length
    ? `No coordinates: ${unknown.join(", ")}`
    : wps.length
      ? `${wps.length} waypoint(s)`
      : "No waypoints — type a route and Plot.";
  if (latlngs.length >= 2) {
    map.fitBounds(routeLine.getBounds().pad(0.2));
  }
  syncFixLabels();
}

function drawFixes(rows) {
  state.fixes = rows || [];
  fixLayer.clearLayers();
  state.fixes.forEach((f) => {
    if (f.lat == null || f.lon == null) return;
    const isFix = f.kind === "fix";
    const marker = L.circleMarker([f.lat, f.lon], {
      pane: "routePane",
      radius: isFix ? 3 : 2,
      color: isFix ? "#7dd3fc" : "#64748b",
      weight: 1,
      fillColor: isFix ? "#e0f2fe" : "#94a3b8",
      fillOpacity: 0.9,
    });
    const label = f.name || f.id;
    marker.bindTooltip(label, {
      permanent: isFix,
      direction: "right",
      className: "fix-label",
      offset: [6, 0],
      opacity: 0.95,
    });
    marker.addTo(fixLayer);
  });
  syncFixLabels();
}

function syncFixLabels() {
  const on = el("showFixes").checked;
  if (on) {
    if (!map.hasLayer(fixLayer)) map.addLayer(fixLayer);
  } else if (map.hasLayer(fixLayer)) {
    map.removeLayer(fixLayer);
  }
  const showNames = on && map.getZoom() >= 9;
  document.querySelectorAll(".fix-label").forEach((node) => {
    node.style.display = showNames ? "" : "none";
  });
}

async function plotRoute() {
  const q = el("route").value.trim();
  const res = await fetch(
    `/api/tester/route?airport=${encodeURIComponent(state.airport)}&q=${encodeURIComponent(q)}`
  );
  const out = await res.json();
  if (!out.ok) {
    el("routeUnknown").textContent = out.error || "route failed";
    return;
  }
  if (out.hop) el("hop").textContent = out.hop;
  drawRoute(out);
  if ((out.waypoints || []).length) {
    const first = out.waypoints[0];
    jet.setLatLng([first.lat, first.lon]);
    state.playIdx = 0;
    state.playT = 0;
    if (out.waypoints.length > 1) {
      setJetHeading(bearingDeg([first.lat, first.lon], [out.waypoints[1].lat, out.waypoints[1].lon]));
    }
  }
  await sendTick();
}

function playFrame(ts) {
  if (!state.playing) return;
  const wps = state.waypoints;
  if (wps.length < 2) {
    state.playing = false;
    return;
  }
  if (!state.lastTickMs) state.lastTickMs = ts;
  const dt = Math.min(0.25, (ts - state.lastTickMs) / 1000);
  state.lastTickMs = ts;
  const kt = Number(el("spd").value) || 1;
  const nmPerSec = kt / 3600;
  let remain = nmPerSec * dt;
  while (remain > 0 && state.playIdx < wps.length - 1) {
    const a = wps[state.playIdx];
    const b = wps[state.playIdx + 1];
    const seg = haversineNm([a.lat, a.lon], [b.lat, b.lon]) || 0.0001;
    const left = (1 - state.playT) * seg;
    if (remain >= left) {
      remain -= left;
      state.playIdx += 1;
      state.playT = 0;
      jet.setLatLng([b.lat, b.lon]);
      setJetHeading(bearingDeg([a.lat, a.lon], [b.lat, b.lon]));
    } else {
      state.playT += remain / seg;
      remain = 0;
      const lat = lerp(a.lat, b.lat, state.playT);
      const lon = lerp(a.lon, b.lon, state.playT);
      jet.setLatLng([lat, lon]);
      setJetHeading(bearingDeg([a.lat, a.lon], [b.lat, b.lon]));
    }
  }
  if (state.playIdx >= wps.length - 1) {
    const last = wps[wps.length - 1];
    jet.setLatLng([last.lat, last.lon]);
    state.playing = false;
    sendTick();
    return;
  }
  if (!state.lastSendMs || ts - state.lastSendMs > 900) {
    state.lastSendMs = ts;
    sendTick();
  }
  requestAnimationFrame(playFrame);
}

async function loadState(airportKey) {
  const res = await fetch(`/api/tester/state?airport=${encodeURIComponent(airportKey || "")}`);
  const out = await res.json();
  state.airport = out.airport;
  const sel = el("airport");
  sel.innerHTML = "";
  (out.airports || []).forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.key;
    opt.textContent = `${a.name}${a.icao ? " (" + a.icao + ")" : ""}`;
    if (a.key === out.airport) opt.selected = true;
    sel.appendChild(opt);
  });
  zoneLayer.clearLayers();
  if (out.geojson) zoneLayer.addData(out.geojson);
  el("route").value = out.default_route || "";
  if (out.callsign && !(el("callsign").value || "").trim()) {
    el("callsign").value = out.callsign;
  } else if (out.callsign && el("callsign").value === "FLEECE 1") {
    el("callsign").value = out.callsign;
  }
  el("hop").textContent = out.hop || "";
  if (out.centre) map.setView(out.centre, 9);
  drawRoute(out.route || { waypoints: [] });
  drawFixes(out.fixes || []);
  if ((out.route && out.route.waypoints && out.route.waypoints[0]) || out.centre) {
    const start = (out.route && out.route.waypoints && out.route.waypoints[0]) || {
      lat: out.centre[0],
      lon: out.centre[1],
    };
    jet.setLatLng([start.lat, start.lon]);
  }
  el("editorLink").href = `/?airport=${encodeURIComponent(state.airport)}`;
  await sendTick();
}

async function refreshOverlays() {
  try {
    const res = await fetch("/api/overlays");
    const out = await res.json();
    const pick = el("overlayPick");
    const keep = pick.value;
    pick.innerHTML = '<option value="">None</option>';
    (out.overlays || []).forEach((row) => {
      const opt = document.createElement("option");
      const id = row.id || row.file || row.name;
      opt.value = id;
      opt.textContent = row.name || id;
      pick.appendChild(opt);
    });
    if (keep) pick.value = keep;
  } catch (err) {
    el("overlayStatus").textContent = String(err);
  }
}

let overlayLayer = null;

async function loadOverlay() {
  const name = el("overlayPick").value;
  if (overlayLayer) {
    map.removeLayer(overlayLayer);
    overlayLayer = null;
  }
  if (!name) {
    el("overlayStatus").textContent = "";
    return;
  }
  el("overlayStatus").textContent = "Loading…";
  try {
    const res = await fetch(`/api/overlay/geojson?file=${encodeURIComponent(name)}`);
    const gj = await res.json();
    overlayLayer = L.geoJSON(gj, {
      pane: "refOverlay",
      style: { color: "#94a3b8", weight: 1, fillOpacity: 0.04 },
    }).addTo(map);
    el("overlayStatus").textContent = `${name}: ${(gj.features || []).length} shapes`;
  } catch (err) {
    el("overlayStatus").textContent = String(err);
  }
}

el("airport").addEventListener("change", () => {
  const url = new URL(location.href);
  url.searchParams.set("airport", el("airport").value);
  history.replaceState(null, "", url);
  loadState(el("airport").value);
});
el("plotRoute").addEventListener("click", plotRoute);
el("showFixes").addEventListener("change", syncFixLabels);
el("showTracks").addEventListener("change", pollTracks);
el("driveFly").addEventListener("change", sendTick);
map.on("zoomend", syncFixLabels);
el("resetJet").addEventListener("click", async () => {
  state.playing = false;
  await fetch("/api/tester/tick", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(tickBody({ reset: true })),
  });
  plotRoute();
});
el("play").addEventListener("click", () => {
  if (state.waypoints.length < 2) {
    plotRoute().then(() => {
      state.playing = true;
      state.lastTickMs = 0;
      requestAnimationFrame(playFrame);
    });
    return;
  }
  state.playing = true;
  state.lastTickMs = 0;
  requestAnimationFrame(playFrame);
});
el("pause").addEventListener("click", () => {
  state.playing = false;
});
el("alt").addEventListener("input", () => {
  syncSliders();
  sendTick();
});
el("spd").addEventListener("input", () => {
  syncSliders();
  sendTick();
});
if (el("hdg")) {
  el("hdg").addEventListener("input", () => {
    setJetHeading(Number(el("hdg").value));
    sendTick();
  });
}
function nudgeHdg(delta) {
  setJetHeading(state.heading + delta);
  sendTick();
}
if (el("hdgLeft")) el("hdgLeft").addEventListener("click", () => nudgeHdg(-15));
if (el("hdgRight")) el("hdgRight").addEventListener("click", () => nudgeHdg(15));
if (el("faceRwy")) {
  el("faceRwy").addEventListener("click", () => {
    if (state.runwayHdg == null) {
      el("driveStatus").textContent = "No runway heading yet — wait for a tick.";
      return;
    }
    setJetHeading(state.runwayHdg);
    sendTick();
  });
}
map.getContainer().addEventListener(
  "wheel",
  (ev) => {
    if (!ev.shiftKey) return;
    ev.preventDefault();
    nudgeHdg(ev.deltaY > 0 ? 5 : -5);
  },
  { passive: false }
);
if (el("trafficAlt")) {
  el("trafficAlt").addEventListener("input", syncSliders);
}
if (el("clickMode")) {
  el("clickMode").addEventListener("change", syncClickMode);
}
if (el("clearTraffic")) {
  el("clearTraffic").addEventListener("click", clearTraffic);
}
document.addEventListener("keydown", (ev) => {
  if (ev.key !== "Delete" && ev.key !== "Backspace") return;
  const tag = (ev.target && ev.target.tagName) || "";
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;
  if (!state.selectedTraffic) return;
  ev.preventDefault();
  removeTraffic(state.selectedTraffic);
});
el("callsign").addEventListener("change", sendTick);
el("overrideWx").addEventListener("change", sendTick);
["windDir", "windKt", "altimeter", "visSm", "ceilingFt"].forEach((id) => {
  el(id).addEventListener("change", () => {
    el("metar").value = "";
    sendTick();
  });
});
el("metar").addEventListener("change", sendTick);
el("missionClock").addEventListener("change", sendTick);
el("todDay").addEventListener("click", () => {
  el("missionClock").value = "1300";
  el("overrideWx").checked = true;
  sendTick();
});
el("todNight").addEventListener("click", () => {
  el("missionClock").value = "2300";
  el("overrideWx").checked = true;
  sendTick();
});
el("pullMetar").addEventListener("click", pullMetar);
el("overlayLoad").addEventListener("click", loadOverlay);
el("opusLoad").addEventListener("click", loadOpusFlights);
el("opusApply").addEventListener("click", applyOpusFlight);
jet.on("dragend", () => {
  state.playing = false;
  sendTick();
});
map.on("click", (ev) => {
  if (ev.originalEvent && ev.originalEvent.target.closest(".leaflet-control")) return;
  const mode = (el("clickMode") && el("clickMode").value) || "own";
  if (mode === "red" || mode === "blue") {
    addTraffic(ev.latlng, mode);
    return;
  }
  jet.setLatLng(ev.latlng);
  state.playing = false;
  sendTick();
});

function trafficPayload() {
  return state.traffic.map((t) => {
    const ll = t.marker ? t.marker.getLatLng() : { lat: t.lat, lng: t.lon };
    return {
      id: t.id,
      lat: ll.lat,
      lon: ll.lng,
      heading_deg: t.heading,
      alt_ft_agl: t.alt_ft_agl,
      speed_kt: t.speed_kt,
      coalition: t.coalition,
      callsign: t.callsign,
    };
  });
}

function trafficColor(coalition) {
  return coalition === "red" ? "#ef4444" : "#3b82f6";
}

function refreshTrafficMarker(row) {
  if (!row.marker) return;
  row.marker.setIcon(
    planeIcon(trafficColor(row.coalition), row.heading, row.id === state.selectedTraffic)
  );
}

function renderTrafficList() {
  const box = el("trafficList");
  if (!box) return;
  box.innerHTML = "";
  state.traffic.forEach((t) => {
    const btn = document.createElement("button");
    btn.type = "button";
    btn.className = t.coalition + (t.id === state.selectedTraffic ? " on" : "");
    const kft = Math.round(Number(t.alt_ft_agl) / 1000);
    btn.textContent = `${t.callsign} · ${t.coalition} · ${kft}k`;
    btn.addEventListener("click", () => selectTraffic(t.id));
    box.appendChild(btn);
  });
}

function selectTraffic(id) {
  state.selectedTraffic = state.selectedTraffic === id ? "" : id;
  state.traffic.forEach(refreshTrafficMarker);
  renderTrafficList();
}

function addTraffic(latlng, coalition) {
  const n = state.traffic.filter((t) => t.coalition === coalition).length + 1;
  const row = {
    id: `map-traffic-${state.trafficSeq++}`,
    callsign: coalition === "red" ? `BANDIT ${n}` : `FRIENDLY ${n}`,
    coalition,
    heading: state.heading,
    alt_ft_agl: Number(el("trafficAlt").value) || 20000,
    speed_kt: 420,
    marker: null,
  };
  const marker = L.marker(latlng, {
    icon: planeIcon(trafficColor(coalition), row.heading, false),
    draggable: true,
    zIndexOffset: 800,
  }).addTo(trafficLayer);
  marker.bindTooltip(`${row.callsign} · ${row.coalition}`);
  marker.on("click", (ev) => {
    L.DomEvent.stopPropagation(ev);
    selectTraffic(row.id);
  });
  marker.on("dragend", () => sendTick());
  row.marker = marker;
  state.traffic.push(row);
  state.selectedTraffic = row.id;
  state.traffic.forEach(refreshTrafficMarker);
  renderTrafficList();
  sendTick();
}

function removeTraffic(id) {
  const idx = state.traffic.findIndex((t) => t.id === id);
  if (idx < 0) return;
  const row = state.traffic[idx];
  if (row.marker) trafficLayer.removeLayer(row.marker);
  state.traffic.splice(idx, 1);
  if (state.selectedTraffic === id) state.selectedTraffic = "";
  renderTrafficList();
  sendTick();
}

function clearTraffic() {
  state.traffic.forEach((t) => {
    if (t.marker) trafficLayer.removeLayer(t.marker);
  });
  state.traffic = [];
  state.selectedTraffic = "";
  renderTrafficList();
  sendTick();
}

function syncClickMode() {
  const mode = (el("clickMode") && el("clickMode").value) || "own";
  const node = document.getElementById("map");
  if (!node) return;
  node.classList.toggle("place-red", mode === "red");
  node.classList.toggle("place-blue", mode === "blue");
}

function flightLabel(row) {
  const bits = [row.callsign || `#${row.id}`];
  if (row.mission) bits.push(row.mission);
  bits.push(row.route || "(no filed route)");
  return bits.join(" — ");
}

async function loadOpusFlights() {
  el("opusStatus").textContent = "Loading…";
  try {
    const res = await fetch("/api/tester/opus-flights");
    const out = await res.json();
    const sel = el("opusFlight");
    sel.innerHTML = "";
    if (!out.ok) {
      sel.innerHTML = '<option value="">(unavailable)</option>';
      el("opusStatus").textContent = out.error || "Opus unavailable";
      state.opusFlights = [];
      return;
    }
    state.opusFlights = out.flights || [];
    if (!state.opusFlights.length) {
      sel.innerHTML = '<option value="">No flights</option>';
      el("opusStatus").textContent = "Opus returned no flights.";
      return;
    }
    state.opusFlights.forEach((row, i) => {
      const opt = document.createElement("option");
      opt.value = String(i);
      opt.textContent = flightLabel(row);
      sel.appendChild(opt);
    });
    el("opusStatus").textContent = `${state.opusFlights.length} flight(s)`;
  } catch (err) {
    el("opusStatus").textContent = String(err);
  }
}

async function applyOpusFlight() {
  const idx = Number(el("opusFlight").value);
  let row = state.opusFlights[idx];
  if (!row) {
    el("opusStatus").textContent = "Load flights first, then pick one.";
    return;
  }
  el("opusStatus").textContent = "Loading route…";
  if (row.id && !row.route) {
    try {
      const res = await fetch(`/api/tester/opus-flights?id=${encodeURIComponent(row.id)}`);
      const out = await res.json();
      const detailed = (out.flights || [])[0];
      if (out.ok && detailed) {
        row = Object.assign({}, row, detailed);
        state.opusFlights[idx] = row;
      } else if (!out.ok) {
        el("opusStatus").textContent = out.error || "Could not load that flight.";
        return;
      }
    } catch (err) {
      el("opusStatus").textContent = String(err);
      return;
    }
  }
  if (row.callsign) el("callsign").value = row.callsign;
  state.fpAltitude = row.altitude || "";
  if (row.route) {
    el("route").value = row.route;
    el("opusStatus").textContent = `Using ${row.callsign || "flight"} — Fly follows this route when map is your jet.`;
    plotRoute();
  } else {
    el("opusStatus").textContent = "That flight has no filed route — type one above.";
    sendTick();
  }
}

async function pullMetar() {
  el("wxHint").textContent = "Pulling Opus METAR…";
  try {
    const res = await fetch(
      `/api/tester/metar?airport=${encodeURIComponent(state.airport)}`
    );
    const out = await res.json();
    if (!out.ok) {
      el("wxHint").textContent = out.error || "METAR unavailable";
      return;
    }
    if (out.metar) el("metar").value = out.metar;
    if (out.wind_dir != null) el("windDir").value = out.wind_dir;
    if (out.wind_speed_kt != null) el("windKt").value = out.wind_speed_kt;
    if (out.altimeter_inhg != null) el("altimeter").value = out.altimeter_inhg;
    if (out.visibility_sm != null) el("visSm").value = out.visibility_sm;
    if (out.ceiling_ft != null) el("ceilingFt").value = out.ceiling_ft;
    else el("ceilingFt").value = "";
    sendTick();
  } catch (err) {
    el("wxHint").textContent = String(err);
  }
}

syncSliders();
syncClickMode();
loadState(qsAirport());
refreshOverlays();
setInterval(pollTracks, 2000);
pollTracks();
