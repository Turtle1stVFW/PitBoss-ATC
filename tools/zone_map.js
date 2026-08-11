/*
 * Runway zone editor. Internal tool — see tools/README.md.
 *
 * The map is already georeferenced, so a drawn shape needs no calibration: its
 * lat/lon go straight into airports.json, and runway_position converts them to
 * feed metres at runtime.
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
  other: "#94a3b8",
};
const TRIGGER_LABELS = {
  runway: "Runway centreline (line)",
  in_position: "In position — cleared for takeoff",
  eor: "EOR — monitor tower",
  hold_short: "Hold short",
  parking: "Parking / ramp",
  delivery: "Clearance delivery area",
  ground: "Ground area",
  tower: "Tower area",
  departure: "Departure area",
  approach: "Approach area",
  other: "Other",
};
const CUSTOM = "__custom__";
const TRACK_POLL_MS = 1500;

const el = (id) => document.getElementById(id);
const colourFor = (trigger) => COLOURS[trigger] || COLOURS.other;

const state = {
  airport: "",
  runways: [],
  selected: null,
  seq: 0,
  dirty: false,
  clipboard: null, // one GeoJSON Feature — a zone is exactly one shape
};

const map = L.map("map", { zoomControl: true, maxZoom: 20 });
// Reference packs sit under editable zones so your shapes stay clickable.
map.createPane("refOverlay");
map.getPane("refOverlay").style.zIndex = 350;
// Tiles come from this same localhost server (/tiles/...), which fetches Esri
// or OSM on the browser's behalf. Direct third-party tile URLs from a
// 127.0.0.1 page are often blocked, leaving a blank black map.
const satellite = L.tileLayer("/tiles/esri/{z}/{y}/{x}", {
  maxZoom: 20,
  maxNativeZoom: 19,
  attribution: "Imagery &copy; Esri, Maxar, Earthstar Geographics",
}).addTo(map);
const labels = L.tileLayer("/tiles/esri-labels/{z}/{y}/{x}", {
  maxZoom: 20,
  maxNativeZoom: 19,
  opacity: 0.85,
});
const streets = L.tileLayer("/tiles/osm/{z}/{x}/{y}.png", {
  maxZoom: 19,
  attribution: "&copy; OpenStreetMap contributors",
});
L.control
  .layers({ Satellite: satellite, Streets: streets }, { "Place names": labels })
  .addTo(map);
L.control.scale({ imperial: true, metric: false }).addTo(map);

// Aviation units on the draw tooltips: feet up close, NM when it gets larger.
// Storage stays metres (radius_m, etc.); only what you read on screen changes.
const FT_PER_M = 3.280839895;
const M_PER_NM = 1852;

function formatLengthM(meters) {
  const m = Number(meters) || 0;
  const nm = m / M_PER_NM;
  if (nm >= 0.1) {
    const digits = nm >= 10 ? 0 : nm >= 1 ? 1 : 2;
    return `${nm.toFixed(digits)} NM`;
  }
  return `${Math.round(m * FT_PER_M)} ft`;
}

function formatAreaM2(sqMeters) {
  const a = Number(sqMeters) || 0;
  const nm2 = a / (M_PER_NM * M_PER_NM);
  if (nm2 >= 0.01) {
    return `${nm2.toFixed(nm2 >= 1 ? 2 : 3)} NM²`;
  }
  const acres = a / 4046.8564224;
  if (acres >= 0.1) {
    return `${acres.toFixed(acres >= 10 ? 0 : 1)} acres`;
  }
  return `${Math.round(a * 10.763910417)} ft²`;
}

if (L.GeometryUtil) {
  L.GeometryUtil.readableDistance = function (distance) {
    return formatLengthM(distance);
  };
  L.GeometryUtil.readableArea = function (area) {
    return formatAreaM2(area);
  };
}

let tileErrors = 0;
function onTileError() {
  tileErrors += 1;
  if (tileErrors !== 3) return;
  setStatus(
    "Map imagery is not loading. Check that this machine can reach the internet, " +
      "then reload. The Streets layer (top-right layers control) is a second try.",
    "err"
  );
}
satellite.on("tileerror", onTileError);
streets.on("tileerror", onTileError);
satellite.on("tileload", () => {
  if (tileErrors) tileErrors = 0;
});

const drawn = new L.FeatureGroup().addTo(map);
const trackLayer = new L.LayerGroup().addTo(map);
const overlayRoot = new L.LayerGroup().addTo(map);
const overlayCanvas = L.canvas({ padding: 0.5, pane: "refOverlay" });
const overlayHighlight = new L.LayerGroup().addTo(map);
const overlayState = {
  id: null,
  name: "",
  groups: new Map(), // folder id -> LayerGroup
  enabled: new Map(), // folder id -> bool
  master: true,
  tree: null, // nested folder tree
  index: [], // searchable placemarks { name, nameLower, folder, type, layer }
  hitIndex: -1,
  expanded: new Set(), // folder paths that are open in the tree UI
};
let overlaySearchTimer = null;
let overlayPulseTimer = null;
const OVERLAY_HIT_LIMIT = 40;

map.addControl(
  new L.Control.Draw({
    position: "topleft",
    draw: {
      // metric:false + our GeometryUtil overrides → ft / NM while drawing
      polygon: { showArea: true, metric: false, feet: true, allowIntersection: false },
      rectangle: { showArea: true, metric: false, feet: true },
      circle: { metric: false, feet: true },
      polyline: { metric: false, feet: true },
      marker: false,
      circlemarker: false,
    },
    // No Leaflet.Draw edit/trash toolbar. Its "Clear All" wipes every zone, and
    // vertex handles already appear on the selected zone. Delete is the list ×,
    // Delete zone, or the Delete key — always the selected zone only.
  })
);

/* --- shapes ---------------------------------------------------------- */

/* The sidebar form is both the properties panel for the selected shape and the
 * template for the next one drawn, so what you set up to draw a shape is what you
 * come back to when you click it. */

function normaliseTrigger(raw) {
  return String(raw || "")
    .trim()
    .toLowerCase()
    .replace(/[\s-]+/g, "_");
}

function formTrigger() {
  const picked = el("trigger").value;
  if (picked !== CUSTOM) return picked;
  return normaliseTrigger(el("customTrigger").value) || "other";
}

function altFromInput(id) {
  const raw = el(id).value.trim();
  if (!raw) return null;
  const num = Number(raw);
  return Number.isFinite(num) ? num : null;
}

function tagsFromSidebar() {
  const trigger = formTrigger();
  const runway = el("runway").value;
  const name = el("name").value.trim();
  return {
    id: "",
    trigger,
    runway,
    name: name || defaultName(trigger, runway),
    min_alt_ft: altFromInput("minAlt"),
    max_alt_ft: altFromInput("maxAlt"),
  };
}

function fillForm(props) {
  const trigger = props.trigger || "other";
  const known = Array.from(el("trigger").options).some(
    (o) => o.value === trigger && o.value !== CUSTOM
  );
  el("trigger").value = known ? trigger : CUSTOM;
  el("customTrigger").value = known ? "" : trigger;
  el("runway").value = props.runway || "";
  el("name").value = props.name || "";
  el("minAlt").value = props.min_alt_ft == null ? "" : props.min_alt_ft;
  el("maxAlt").value = props.max_alt_ft == null ? "" : props.max_alt_ft;
  syncCustomTrigger();
}

function syncCustomTrigger() {
  el("customTrigger").classList.toggle("hidden", el("trigger").value !== CUSTOM);
}

function isLocked(layer) {
  return Boolean(layer && layer.zoneProps && layer.zoneProps.locked);
}

function setFormFieldsDisabled(disabled) {
  ["trigger", "customTrigger", "runway", "name", "minAlt", "maxAlt"].forEach((id) => {
    const node = el(id);
    if (node) node.disabled = Boolean(disabled);
  });
  ["rotateLeft", "rotateRight"].forEach((id) => {
    const node = el(id);
    if (node) node.disabled = Boolean(disabled);
  });
}

function syncLockChrome(layer) {
  const locked = isLocked(layer);
  const lockRow = el("lockRow");
  if (lockRow) lockRow.classList.toggle("hidden", !layer);
  const lockBtn = el("lockZone");
  const unlockBtn = el("unlockZone");
  if (lockBtn) lockBtn.classList.toggle("hidden", !layer || locked);
  if (unlockBtn) unlockBtn.classList.toggle("hidden", !layer || !locked);
  const del = el("deleteZone");
  if (del) {
    del.disabled = locked;
    del.title = locked
      ? "Unlock this zone before deleting"
      : "Delete the selected zone only (Delete)";
  }
  setFormFieldsDisabled(locked);
}

/* Editing the form with a shape selected changes that shape; with nothing
 * selected it is just setting up the next one. */
function onFormEdit() {
  syncCustomTrigger();
  const layer = findLayer(state.selected);
  if (!layer) {
    updateEditHint(null);
    return;
  }
  if (isLocked(layer)) {
    fillForm(layer.zoneProps);
    setStatus("This zone is locked — Unlock… before editing tags or the shape.", "warn");
    return;
  }
  const keptId = layer.zoneProps.id || "";
  const keptLocked = Boolean(layer.zoneProps.locked);
  const tags = tagsFromSidebar();
  delete tags.id;
  delete tags.locked;
  Object.assign(layer.zoneProps, tags);
  if (keptId) layer.zoneProps.id = keptId;
  layer.zoneProps.locked = keptLocked;
  styleLayer(layer);
  refreshList();
  markDirty();
  updateEditHint(layer);
}

function updateEditHint(layer) {
  const hint = el("editHint");
  if (!hint) return;
  if (!layer) {
    hint.innerHTML =
      "No zone selected. Click one in the list to rename or retag it, or draw a " +
      "new one with the tools on the map. <strong>Save to airports.json</strong> " +
      "writes the file.";
    return;
  }
  const name = layer.zoneProps.name || "(unnamed)";
  if (isLocked(layer)) {
    hint.innerHTML =
      `<strong>${escapeHtml(name)}</strong> is <strong>locked</strong> (default). ` +
      `Shape and tags cannot change until you press <strong>Unlock…</strong>.`;
    return;
  }
  hint.innerHTML =
    `Editing <strong>${escapeHtml(name)}</strong>. Changes show in the list ` +
    `immediately — press <strong>Save to airports.json</strong> to keep them. ` +
    `Use <strong>Lock zone</strong> when it should be a protected default.`;
}

function escapeHtml(text) {
  return String(text)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

function syncSaveButton() {
  const btn = el("save");
  if (!btn) return;
  btn.classList.toggle("dirty", state.dirty);
  btn.textContent = state.dirty
    ? "Save changes to airports.json"
    : "Save to airports.json";
  const hint = el("saveHint");
  if (hint) {
    hint.textContent = state.dirty
      ? "You have unsaved edits (names, shapes, new zones). Save before closing or switching airport."
      : "Saves every zone for this field (renames, moves, new draws). Do this before you close the editor or switch airport.";
  }
}

function defaultName(trigger, runway) {
  if (trigger === "runway") return `Runway ${runway}`.trim();
  const base = trigger.replace(/_/g, " ");
  return runway ? `${runway} ${base}` : base;
}

function bandText(props) {
  if (props.min_alt_ft == null && props.max_alt_ft == null) return "";
  const lo = props.min_alt_ft ? `${props.min_alt_ft}` : "sfc";
  const hi = props.max_alt_ft == null ? "unlim" : `${props.max_alt_ft}`;
  return `${lo}-${hi} ft`;
}

function styleLayer(layer) {
  if (!layerVisible(layer)) {
    layer.options.interactive = false;
    if (layer._path) layer._path.setAttribute("pointer-events", "none");
    if (layer.setStyle) layer.setStyle({ opacity: 0, fillOpacity: 0, weight: 0 });
    return;
  }
  layer.options.interactive = true;
  if (layer._path) layer._path.setAttribute("pointer-events", "auto");
  const props = layer.zoneProps || {};
  const colour = colourFor(props.trigger);
  const chosen = layer.zoneKey === state.selected;
  const locked = isLocked(layer);
  const isLine = layer instanceof L.Polyline && !(layer instanceof L.Polygon);
  if (layer.setStyle) {
    layer.setStyle({
      color: chosen ? "#ffffff" : colour,
      weight: (props.trigger === "runway" ? 4 : 2) + (chosen ? 2 : 0),
      opacity: locked ? 0.75 : 0.95,
      fillColor: colour,
      fillOpacity: isLine ? 0 : chosen ? 0.35 : locked ? 0.12 : 0.18,
      dashArray:
        props.trigger === "hold_short" ? "6,5" : locked ? "2,6" : null,
    });
  }
  const parts = [props.name || "(unnamed)"];
  if (props.runway) parts.push(props.runway);
  if (bandText(props)) parts.push(bandText(props));
  layer.bindTooltip(parts.join(" · "), { sticky: true });
}

function register(layer, props) {
  state.seq += 1;
  layer.zoneProps = Object.assign(
    {
      trigger: "other",
      runway: "",
      name: "",
      min_alt_ft: null,
      max_alt_ft: null,
      locked: false,
    },
    props
  );
  layer.zoneProps.locked = Boolean(layer.zoneProps.locked);
  layer.zoneKey = `k${state.seq}`;
  layer.zoneHidden = Boolean(layer.zoneHidden);
  drawn.addLayer(layer);
  styleLayer(layer);
  layer.on("edit", onLayerEdited);
  // Clicking a shape on the map highlights it without moving the view; jumping
  // the map while you are working on a shape is worse than useless.
  layer.on("click", () => select(layer.zoneKey, { zoom: false }));
  return layer;
}

function layers() {
  return drawn.getLayers();
}

function findLayer(key) {
  return layers().find((l) => l.zoneKey === key) || null;
}

function setVertexEditing(layer, on) {
  // Leaflet.Draw hangs an `.editing` handler on polygons, lines and circles.
  // Enabling it puts draggable corner markers and midpoint "add" handles on the
  // shape; clicking a corner deletes it (down to 3 points on a polygon, 2 on a line).
  if (!layer || !layer.editing) return;
  if (on && !layerVisible(layer)) on = false;
  try {
    if (on) {
      if (!layer.editing.enabled()) layer.editing.enable();
    } else if (layer.editing.enabled()) {
      layer.editing.disable();
    }
  } catch (err) {
    console.warn("vertex edit", err);
  }
}

function layerVisible(layer) {
  return Boolean(layer && !layer.zoneHidden);
}

function setZoneHidden(layer, hidden) {
  if (!layer) return;
  layer.zoneHidden = Boolean(hidden);
  applyZoneVisibility();
  refreshList();
}

function setAllZonesHidden(hidden) {
  layers().forEach((layer) => {
    layer.zoneHidden = Boolean(hidden);
  });
  applyZoneVisibility();
  refreshList();
}

function applyZoneVisibility() {
  // Shapes stay in the FeatureGroup so save/list still see them; only rendering
  // and edit handles follow the per-zone eye toggles (not written to airports.json).
  layers().forEach((layer) => {
    const show = layerVisible(layer);
    layer.options.interactive = show;
    if (layer._path) {
      layer._path.setAttribute("pointer-events", show ? "auto" : "none");
    }
    if (show) {
      styleLayer(layer);
    } else if (layer.setStyle) {
      layer.setStyle({ opacity: 0, fillOpacity: 0, weight: 0 });
    }
  });

  const selected = findLayer(state.selected);
  layers().forEach((l) => setVertexEditing(l, false));
  mover.detach();
  rotation.detach();
  if (selected && layerVisible(selected) && !isLocked(selected)) {
    setVertexEditing(selected, true);
    mover.attach(selected);
    rotation.attach(selected);
  }
}

function onLayerEdited() {
  if (this && isLocked(this)) {
    setStatus("This zone is locked — Unlock… before reshaping.", "warn");
    return;
  }
  markDirty();
  refreshList();
  if (this && this.zoneKey) styleLayer(this);
  if (this && this.zoneKey === state.selected) {
    mover.reseat();
    rotation.reseat();
  }
}

/* --- rotation -------------------------------------------------------- */

const M_PER_DEG_LAT = 111320;

function ringOf(layer) {
  if (!layer || layer instanceof L.Circle || !(layer instanceof L.Polyline)) return null;
  const raw = layer.getLatLngs();
  const ring = layer instanceof L.Polygon && Array.isArray(raw[0]) ? raw[0] : raw;
  return ring.map((p) => L.latLng(p));
}

function setRing(layer, ring) {
  if (layer instanceof L.Polygon) {
    const rest = layer.getLatLngs().slice(1);
    layer.setLatLngs([ring, ...rest]);
  } else {
    layer.setLatLngs(ring);
  }
}

function centroidOf(ring) {
  let lat = 0;
  let lng = 0;
  ring.forEach((p) => {
    lat += p.lat;
    lng += p.lng;
  });
  return L.latLng(lat / ring.length, lng / ring.length);
}

function toXY(ll, origin) {
  const cos = Math.cos((origin.lat * Math.PI) / 180);
  return {
    x: (ll.lng - origin.lng) * M_PER_DEG_LAT * cos,
    y: (ll.lat - origin.lat) * M_PER_DEG_LAT,
  };
}

function fromXY(xy, origin) {
  const cos = Math.cos((origin.lat * Math.PI) / 180);
  return L.latLng(
    origin.lat + xy.y / M_PER_DEG_LAT,
    origin.lng + xy.x / (M_PER_DEG_LAT * Math.max(cos, 1e-6))
  );
}

function rotateRing(ring, origin, angleRad) {
  const c = Math.cos(angleRad);
  const s = Math.sin(angleRad);
  return ring.map((p) => {
    const xy = toXY(p, origin);
    return fromXY({ x: xy.x * c - xy.y * s, y: xy.x * s + xy.y * c }, origin);
  });
}

function bearingOf(ll, origin) {
  const xy = toXY(ll, origin);
  return Math.atan2(xy.x, xy.y); // 0 = north
}

const rotation = {
  layer: null,
  handle: null,
  spoke: null,
  center: null,
  originRing: null,
  startBearing: 0,
  dragging: false,

  canRotate(layer) {
    const ring = ringOf(layer);
    return Boolean(ring && ring.length >= 2);
  },

  attach(layer) {
    this.detach();
    if (!this.canRotate(layer)) return;
    this.layer = layer;
    this.reseat();
  },

  reseat() {
    if (!this.layer || this.dragging) return;
    const ring = ringOf(this.layer);
    if (!ring) {
      this.detach();
      return;
    }
    this.center = centroidOf(ring);
    const tip = this._tipLatLng(this.center);
    if (!this.handle) {
      this.handle = L.marker(tip, {
        draggable: true,
        zIndexOffset: 1000,
        icon: L.divIcon({
          className: "rotate-handle",
          html: "↻",
          iconSize: [28, 28],
          iconAnchor: [14, 14],
        }),
      });
      this.handle.on("dragstart", (ev) => this._onStart(ev));
      this.handle.on("drag", (ev) => this._onDrag(ev));
      this.handle.on("dragend", () => this._onEnd());
      this.handle.on("mousedown", L.DomEvent.stopPropagation);
      this.handle.on("click", L.DomEvent.stopPropagation);
      this.handle.addTo(map);
      this.spoke = L.polyline([this.center, tip], {
        className: "rotate-spoke",
        interactive: false,
        weight: 2,
        color: "#ffffff",
        dashArray: "4 4",
        opacity: 0.85,
      }).addTo(map);
    } else {
      this.handle.setLatLng(tip);
      this.spoke.setLatLngs([this.center, tip]);
    }
  },

  detach() {
    this.dragging = false;
    this.layer = null;
    this.originRing = null;
    this.center = null;
    if (this.handle) {
      map.removeLayer(this.handle);
      this.handle = null;
    }
    if (this.spoke) {
      map.removeLayer(this.spoke);
      this.spoke = null;
    }
  },

  nudge(deg) {
    const layer = this.layer || findLayer(state.selected);
    if (!this.canRotate(layer) || isLocked(layer)) return;
    const ring = ringOf(layer);
    const center = centroidOf(ring);
    const was = layer.editing && layer.editing.enabled();
    if (was) setVertexEditing(layer, false);
    setRing(layer, rotateRing(ring, center, (deg * Math.PI) / 180));
    if (was) setVertexEditing(layer, true);
    styleLayer(layer);
    markDirty();
    refreshList();
    if (this.layer === layer) this.reseat();
    else {
      this.attach(layer);
    }
    mover.reseat();
  },

  _tipLatLng(center) {
    const pt = map.latLngToLayerPoint(center);
    return map.layerPointToLatLng(L.point(pt.x, pt.y - 52));
  },

  _onStart() {
    const ring = ringOf(this.layer);
    if (!ring) return;
    this.dragging = true;
    this.originRing = ring.map((p) => L.latLng(p));
    this.center = centroidOf(this.originRing);
    this.startBearing = bearingOf(this.handle.getLatLng(), this.center);
    setVertexEditing(this.layer, false);
  },

  _onDrag() {
    if (!this.dragging || !this.originRing) return;
    const now = bearingOf(this.handle.getLatLng(), this.center);
    const delta = now - this.startBearing;
    setRing(this.layer, rotateRing(this.originRing, this.center, delta));
    styleLayer(this.layer);
    this.spoke.setLatLngs([this.center, this.handle.getLatLng()]);
  },

  _onEnd() {
    this.dragging = false;
    this.originRing = null;
    markDirty();
    refreshList();
    if (this.layer) {
      setVertexEditing(this.layer, true);
      this.reseat();
      mover.reseat();
    }
  },
};

/* --- move (whole shape) ---------------------------------------------- */

function shapeCenter(layer) {
  if (!layer) return null;
  if (layer instanceof L.Circle) return layer.getLatLng();
  const ring = ringOf(layer);
  return ring && ring.length ? centroidOf(ring) : null;
}

const mover = {
  layer: null,
  handle: null,
  dragging: false,
  startLatLng: null,
  originRing: null,
  originCenter: null,
  _onBodyDown: null,
  _onBodyMove: null,
  _onBodyUp: null,

  canMove(layer) {
    return Boolean(shapeCenter(layer));
  },

  attach(layer) {
    this.detach();
    if (!this.canMove(layer)) return;
    this.layer = layer;
    this._onBodyDown = (ev) => this._bodyStart(ev);
    this._onBodyMove = (ev) => this._bodyMove(ev);
    this._onBodyUp = () => this._bodyEnd();
    layer.on("mousedown", this._onBodyDown);
    this.reseat();
  },

  reseat() {
    if (!this.layer || this.dragging) return;
    const center = shapeCenter(this.layer);
    if (!center) {
      this.detach();
      return;
    }
    if (!this.handle) {
      this.handle = L.marker(center, {
        draggable: true,
        zIndexOffset: 1100,
        icon: L.divIcon({
          className: "move-handle",
          html: "✚",
          iconSize: [28, 28],
          iconAnchor: [14, 14],
        }),
      });
      this.handle.on("dragstart", () => this._handleStart());
      this.handle.on("drag", () => this._handleDrag());
      this.handle.on("dragend", () => this._handleEnd());
      this.handle.on("mousedown", L.DomEvent.stopPropagation);
      this.handle.on("click", L.DomEvent.stopPropagation);
      this.handle.addTo(map);
    } else {
      this.handle.setLatLng(center);
    }
  },

  detach() {
    this._bodyEnd(true);
    if (this.layer && this._onBodyDown) {
      this.layer.off("mousedown", this._onBodyDown);
    }
    this.dragging = false;
    this.layer = null;
    this.originRing = null;
    this.originCenter = null;
    this.startLatLng = null;
    this._onBodyDown = null;
    this._onBodyMove = null;
    this._onBodyUp = null;
    if (this.handle) {
      map.removeLayer(this.handle);
      this.handle = null;
    }
  },

  _snapshot() {
    if (this.layer instanceof L.Circle) {
      this.originCenter = L.latLng(this.layer.getLatLng());
      this.originRing = null;
    } else {
      this.originRing = ringOf(this.layer).map((p) => L.latLng(p));
      this.originCenter = null;
    }
  },

  _applyDelta(from, to) {
    const dLat = to.lat - from.lat;
    const dLng = to.lng - from.lng;
    if (this.layer instanceof L.Circle && this.originCenter) {
      this.layer.setLatLng([this.originCenter.lat + dLat, this.originCenter.lng + dLng]);
      return;
    }
    if (this.originRing) {
      setRing(
        this.layer,
        this.originRing.map((p) => L.latLng(p.lat + dLat, p.lng + dLng))
      );
    }
  },

  _begin() {
    this.dragging = true;
    this._snapshot();
    setVertexEditing(this.layer, false);
    // Rotation handle would sit on a stale centre while we slide the shape.
    if (rotation.layer === this.layer) rotation.detach();
  },

  _finish() {
    this.dragging = false;
    this.originRing = null;
    this.originCenter = null;
    this.startLatLng = null;
    markDirty();
    refreshList();
    styleLayer(this.layer);
    setVertexEditing(this.layer, true);
    this.reseat();
    rotation.attach(this.layer);
  },

  _handleStart() {
    if (!this.layer) return;
    this._begin();
    this.startLatLng = this.handle.getLatLng();
  },

  _handleDrag() {
    if (!this.dragging || !this.startLatLng) return;
    this._applyDelta(this.startLatLng, this.handle.getLatLng());
    styleLayer(this.layer);
  },

  _handleEnd() {
    if (!this.dragging) return;
    this._finish();
  },

  _bodyStart(ev) {
    if (!this.layer || this.layer.zoneKey !== state.selected || isLocked(this.layer)) return;
    if (ev.originalEvent && ev.originalEvent.button != null && ev.originalEvent.button !== 0) {
      return;
    }
    // Vertex markers are separate; a mousedown on the fill/stroke moves the whole shape.
    L.DomEvent.stopPropagation(ev);
    L.DomEvent.preventDefault(ev);
    this._begin();
    this.startLatLng = ev.latlng;
    map.dragging.disable();
    map.on("mousemove", this._onBodyMove);
    map.on("mouseup", this._onBodyUp);
  },

  _bodyMove(ev) {
    if (!this.dragging || !this.startLatLng) return;
    this._applyDelta(this.startLatLng, ev.latlng);
    styleLayer(this.layer);
    const c = shapeCenter(this.layer);
    if (this.handle && c) this.handle.setLatLng(c);
  },

  _bodyEnd(silent) {
    if (this._onBodyMove) {
      map.off("mousemove", this._onBodyMove);
      map.off("mouseup", this._onBodyUp);
    }
    try {
      map.dragging.enable();
    } catch (err) {
      /* map may already be tearing down */
    }
    if (silent || !this.dragging) {
      this.dragging = false;
      return;
    }
    this._finish();
  },
};

function select(key, { zoom = true } = {}) {
  layers().forEach((l) => setVertexEditing(l, false));
  mover.detach();
  rotation.detach();
  state.selected = key;
  applyZoneVisibility();
  refreshList();
  const layer = findLayer(key);
  showFormFor(layer);
  if (!layer || !zoom) return;
  if (layer instanceof L.Circle || (layer.getLatLngs && ringOf(layer))) {
    const b = layersBounds([layer]);
    if (b.isValid()) map.fitBounds(b.pad(0.5), { maxZoom: 18 });
  } else if (layer.getLatLng) {
    map.setView(layer.getLatLng(), Math.max(map.getZoom(), 16));
  }
  if (layerVisible(layer) && !isLocked(layer)) {
    mover.reseat();
    rotation.reseat();
  }
}

function showFormFor(layer) {
  const section = el("formTitle").parentElement;
  section.classList.toggle("editing", Boolean(layer));
  section.classList.toggle("locked", isLocked(layer));
  el("deselect").classList.toggle("hidden", !layer);
  el("deleteZone").classList.toggle("hidden", !layer);
  const rotateRow = el("rotateRow");
  if (rotateRow) {
    rotateRow.classList.toggle(
      "hidden",
      !layer || isLocked(layer) || !rotation.canRotate(layer)
    );
  }
  const copyRow = el("copyRow");
  if (copyRow) copyRow.classList.toggle("hidden", !layer);
  syncLockChrome(layer);
  updateEditHint(layer);
  if (layer) {
    el("formTitle").textContent = isLocked(layer) ? "Locked zone" : "Selected zone";
    if (isLocked(layer)) {
      el("formHint").textContent =
        "This is a protected default. Unlock it if you really need to reshape, " +
        "retitle, or delete it — then lock it again when you are done.";
    } else if (layer instanceof L.Circle) {
      el("formHint").textContent =
        "Drag the ✚ handle (or the circle itself) to move it; drag the edge to resize. " +
        "New zone starts a separate one; Delete zone removes only this.";
    } else {
      el("formHint").textContent =
        "Drag the ✚ handle or the filled area to move the whole zone. " +
        "Drag a solid corner to reshape; faint midpoints add corners; click a corner to delete it. " +
        "Drag ↻ to rotate.";
    }
    fillForm(layer.zoneProps);
    return;
  }
  setFormFieldsDisabled(false);
  el("formTitle").textContent = "New zone";
  el("formHint").textContent =
    "Set trigger / runway / label, then draw with a tool on the left of the map. " +
    "Polygon or rectangle for an area, circle for the EOR, line for a runway " +
    "centreline (threshold first). Each zone is one shape.";
}

function removeLayer(key) {
  const layer = findLayer(key);
  if (!layer) return false;
  if (isLocked(layer)) {
    setStatus(
      `“${layer.zoneProps.name || "Zone"}” is locked — Unlock… before deleting.`,
      "warn"
    );
    select(key, { zoom: false });
    return false;
  }
  drawn.removeLayer(layer);
  markDirty();
  if (state.selected === key) select(null, { zoom: false });
  else refreshList();
  return true;
}

function lockSelectedZone() {
  const layer = findLayer(state.selected);
  if (!layer) return;
  layer.zoneProps.locked = true;
  applyZoneVisibility();
  showFormFor(layer);
  refreshList();
  markDirty();
  setStatus(`Locked “${layer.zoneProps.name || "zone"}”.`, "ok");
}

function unlockSelectedZone() {
  const layer = findLayer(state.selected);
  if (!layer || !isLocked(layer)) return;
  const label = layer.zoneProps.name || "this zone";
  if (
    !confirm(
      `Unlock “${label}”?\n\nYou can then edit or delete it. Lock it again when you are done so it stays a protected default.`
    )
  ) {
    return;
  }
  layer.zoneProps.locked = false;
  applyZoneVisibility();
  showFormFor(layer);
  refreshList();
  markDirty();
  setStatus(
    `Unlocked “${label}”. Remember to lock it again if it should stay a default.`,
    "warn"
  );
}

function startNewZone() {
  select(null, { zoom: false });
  const prefix = state.dirty
    ? "Unsaved edits are still in memory — Save to airports.json when you are done. "
    : "";
  setStatus(
    prefix +
      "New zone: set trigger / runway / label, then draw with a tool on the left of the map.",
    state.dirty ? "warn" : "ok"
  );
  el("formTitle").parentElement.scrollIntoView({ block: "nearest" });
  el("name").focus();
}

function deleteSelectedZone() {
  const key = state.selected;
  if (!key) {
    setStatus("Select a zone to delete.", "warn");
    return;
  }
  const layer = findLayer(key);
  if (isLocked(layer)) {
    setStatus(`“${layer.zoneProps.name || "Zone"}” is locked — Unlock… before deleting.`, "warn");
    return;
  }
  const label = (layer && layer.zoneProps && layer.zoneProps.name) || "this zone";
  if (!confirm(`Delete “${label}” only?\n\nOther zones are left alone.`)) return;
  if (removeLayer(key)) setStatus(`Deleted “${label}”.`, "ok");
}

function describe(layer) {
  if (layer instanceof L.Circle) return `circle, r ${formatLengthM(layer.getRadius())}`;
  if (layer instanceof L.Polygon) {
    const ring = layer.getLatLngs()[0] || [];
    const area =
      L.GeometryUtil && L.GeometryUtil.geodesicArea
        ? L.GeometryUtil.geodesicArea(ring)
        : null;
    const pts = `${ring.length} pts`;
    return area != null ? `polygon, ${pts}, ${formatAreaM2(area)}` : `polygon, ${pts}`;
  }
  if (layer instanceof L.Polyline) {
    const pts = layer.getLatLngs();
    const len = pts.length > 1 ? pts[0].distanceTo(pts[pts.length - 1]) : 0;
    return `line, ${formatLengthM(len)}`;
  }
  return "point";
}

function refreshList() {
  const list = el("zones");
  list.textContent = "";
  const all = layers();
  el("count").textContent = all.length ? `(${all.length})` : "";
  if (!all.length) {
    const li = document.createElement("li");
    li.className = "empty";
    li.textContent = "Nothing drawn yet.";
    list.appendChild(li);
    return;
  }
  all.forEach((layer) => {
    const props = layer.zoneProps;
    const li = document.createElement("li");
    li.className = layer.zoneKey === state.selected ? "sel" : "";
    if (layer.zoneHidden) li.classList.add("dim");
    if (isLocked(layer)) li.classList.add("locked");

    const sw = document.createElement("span");
    sw.className = "sw";
    sw.style.background = colourFor(props.trigger);
    li.appendChild(sw);

    const txt = document.createElement("span");
    txt.className = "txt";
    const b = document.createElement("b");
    b.textContent = (isLocked(layer) ? "🔒 " : "") + (props.name || "(unnamed)");
    const small = document.createElement("small");
    small.textContent = [
      props.trigger,
      props.runway,
      describe(layer),
      bandText(props),
      isLocked(layer) ? "locked" : "",
    ]
      .filter(Boolean)
      .join(" · ");
    txt.append(b, small);
    txt.addEventListener("click", () => select(layer.zoneKey));
    li.appendChild(txt);

    const eye = document.createElement("button");
    eye.type = "button";
    eye.className = "eye" + (layer.zoneHidden ? " off" : "");
    eye.title = layer.zoneHidden ? "Show on map" : "Hide on map";
    eye.setAttribute("aria-label", eye.title);
    eye.setAttribute("aria-pressed", layer.zoneHidden ? "false" : "true");
    eye.textContent = "👁";
    eye.addEventListener("click", (ev) => {
      ev.stopPropagation();
      setZoneHidden(layer, !layer.zoneHidden);
    });
    li.appendChild(eye);

    const del = document.createElement("button");
    del.className = "del";
    del.title = isLocked(layer) ? "Unlock before deleting" : "Delete";
    del.textContent = isLocked(layer) ? "🔒" : "✕";
    del.disabled = isLocked(layer);
    del.addEventListener("click", (ev) => {
      ev.stopPropagation();
      if (isLocked(layer)) {
        select(layer.zoneKey, { zoom: false });
        setStatus("Unlock… this zone before deleting.", "warn");
        return;
      }
      if (!confirm(`Delete “${props.name || "this zone"}” only?`)) return;
      removeLayer(layer.zoneKey);
    });
    li.appendChild(del);

    list.appendChild(li);
  });
}

function markDirty() {
  state.dirty = true;
  syncSaveButton();
  setStatus("Unsaved changes — press Save to airports.json to keep them.", "warn");
}

map.on(L.Draw.Event.CREATED, (ev) => {
  const props = tagsFromSidebar();
  const isLine = ev.layerType === "polyline";
  // A centreline is the one shape that has to be a line, and a line is only
  // ever a centreline, so keep the two in step rather than saving something the
  // importer will refuse.
  if (props.trigger === "runway" && !isLine) {
    props.trigger = "other";
    props.name = defaultName("other", props.runway);
    setStatus("A runway centreline has to be a line — this was saved as Other.", "err");
  } else if (isLine && props.trigger !== "runway") {
    props.trigger = "runway";
    props.name = defaultName("runway", props.runway);
  } else if (props.trigger === "runway" && !props.runway) {
    setStatus("Pick the runway before drawing its centreline.", "err");
  } else {
    markDirty();
  }
  register(ev.layer, props);
  state.dirty = true;
  select(ev.layer.zoneKey, { zoom: false });
});

// Starting a new draw should drop the current selection so its vertex handles
// do not sit on top of the sketch.
map.on(L.Draw.Event.DRAWSTART, () => {
  if (state.selected) select(null, { zoom: false });
});

map.on("zoomend moveend", () => {
  if (mover.layer && !mover.dragging) mover.reseat();
  if (rotation.layer && !rotation.dragging) rotation.reseat();
});

// Clicking bare map is how you stop editing a shape; interactive layers stop
// their own clicks from reaching here.
map.on("click", () => {
  if (state.selected) select(null, { zoom: false });
});

/* --- GeoJSON in and out ---------------------------------------------- */

function featureFromLayer(layer) {
  const props = Object.assign({}, layer.zoneProps);
  if (layer instanceof L.Circle) {
    props.radius_m = Math.round(layer.getRadius() * 10) / 10;
    const c = layer.getLatLng();
    return {
      type: "Feature",
      properties: props,
      geometry: { type: "Point", coordinates: [c.lng, c.lat] },
    };
  }
  const gj = layer.toGeoJSON();
  // One zone = one outer ring. Drop holes if a polygon somehow acquired them.
  if (gj.geometry && gj.geometry.type === "Polygon" && gj.geometry.coordinates.length > 1) {
    gj.geometry = {
      type: "Polygon",
      coordinates: [gj.geometry.coordinates[0]],
    };
  }
  gj.properties = props;
  return gj;
}

function shiftLatLng(lat, lng, eastM, northM) {
  const cos = Math.max(Math.cos((lat * Math.PI) / 180), 1e-6);
  return [
    lat + northM / M_PER_DEG_LAT,
    lng + eastM / (M_PER_DEG_LAT * cos),
  ];
}

function shiftFeature(feature, eastM, northM) {
  const geom = feature.geometry;
  if (!geom) return feature;
  if (geom.type === "Point") {
    const [lng, lat] = geom.coordinates;
    const [nLat, nLng] = shiftLatLng(lat, lng, eastM, northM);
    geom.coordinates = [nLng, nLat];
  } else if (geom.type === "LineString") {
    geom.coordinates = geom.coordinates.map(([lng, lat]) => {
      const [nLat, nLng] = shiftLatLng(lat, lng, eastM, northM);
      return [nLng, nLat];
    });
  } else if (geom.type === "Polygon") {
    geom.coordinates = [
      geom.coordinates[0].map(([lng, lat]) => {
        const [nLat, nLng] = shiftLatLng(lat, lng, eastM, northM);
        return [nLng, nLat];
      }),
    ];
  }
  return feature;
}

function addFeatureAsLayer(feature) {
  const props = feature.properties || {};
  const geom = feature.geometry || {};
  const tags = {
    id: props.id || "",
    trigger: normaliseTrigger(props.trigger) || "other",
    runway: props.runway || "",
    name: props.name || "",
    min_alt_ft: props.min_alt_ft == null ? null : Number(props.min_alt_ft),
    max_alt_ft: props.max_alt_ft == null ? null : Number(props.max_alt_ft),
    locked: Boolean(props.locked),
  };
  if (!tags.name) tags.name = defaultName(tags.trigger, tags.runway);

  let layer = null;
  if (geom.type === "Point" && props.radius_m) {
    const [lon, lat] = geom.coordinates;
    layer = register(L.circle([lat, lon], { radius: Number(props.radius_m) }), tags);
  } else if (geom.type === "Polygon") {
    const ring = geom.coordinates[0].map(([lon, lat]) => [lat, lon]);
    if (ring.length > 2) {
      const a = ring[0];
      const b = ring[ring.length - 1];
      if (a[0] === b[0] && a[1] === b[1]) ring.pop();
    }
    layer = register(L.polygon(ring), tags);
  } else if (geom.type === "LineString") {
    layer = register(
      L.polyline(geom.coordinates.map(([lon, lat]) => [lat, lon])),
      tags
    );
  }
  return layer;
}

function copySelected() {
  const layer = findLayer(state.selected);
  if (!layer) {
    setStatus("Select a zone to copy.", "warn");
    return;
  }
  const feature = featureFromLayer(layer);
  feature.properties = Object.assign({}, feature.properties);
  delete feature.properties.id; // paste must mint a new zone, not reuse this id
  feature.properties.locked = false; // copies are editable until locked again
  state.clipboard = feature;
  const text = JSON.stringify(feature);
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).catch(() => {});
  }
  setStatus(`Copied “${feature.properties.name}” as one zone. Paste as new to duplicate it.`, "ok");
}

async function pasteAsNew() {
  let feature = state.clipboard ? JSON.parse(JSON.stringify(state.clipboard)) : null;
  let note = "";
  if (navigator.clipboard && navigator.clipboard.readText) {
    try {
      const text = (await navigator.clipboard.readText()).trim();
      if (text.startsWith("{")) {
        const parsed = JSON.parse(text);
        if (parsed.type === "Feature" && parsed.geometry) {
          feature = parsed;
        } else if (parsed.type === "FeatureCollection" && (parsed.features || []).length) {
          feature = parsed.features[0];
          if (parsed.features.length > 1) {
            note = `Clipboard had ${parsed.features.length} shapes; pasted the first as its own zone.`;
          }
        }
      }
    } catch {
      /* keep in-memory clipboard */
    }
  }
  if (!feature || !feature.geometry) {
    setStatus("Nothing to paste. Copy a zone first.", "warn");
    return;
  }

  // Nudge so the paste is not stacked exactly on the original.
  shiftFeature(feature, 80, 80);
  const props = feature.properties || (feature.properties = {});
  props.id = "";
  props.locked = false;
  const base = String(props.name || defaultName(props.trigger || "other", props.runway || "")).trim();
  if (!/\bcopy\b/i.test(base)) props.name = `${base} copy`;

  const layer = addFeatureAsLayer(feature);
  if (!layer) {
    setStatus("Clipboard is not a drawable zone shape.", "err");
    return;
  }
  layer.zoneHidden = false;
  markDirty();
  select(layer.zoneKey, { zoom: false });
  setStatus(note || `Pasted new zone “${layer.zoneProps.name}”.`, note ? "warn" : "ok");
}

function toGeoJSON() {
  return { type: "FeatureCollection", features: layers().map(featureFromLayer) };
}

function loadGeoJSON(collection, { clear }) {
  if (clear) {
    drawn.clearLayers();
    state.selected = null;
    showFormFor(null);
  }
  (collection.features || []).forEach((feat) => {
    const geom = feat.geometry || {};
    // One zone = one shape. Multi* geometries become separate zones.
    if (geom.type === "MultiPolygon") {
      (geom.coordinates || []).forEach((poly) => {
        addFeatureAsLayer({
          type: "Feature",
          properties: Object.assign({}, feat.properties, { id: "" }),
          geometry: { type: "Polygon", coordinates: [poly[0]] },
        });
      });
      return;
    }
    if (geom.type === "MultiLineString") {
      (geom.coordinates || []).forEach((line) => {
        addFeatureAsLayer({
          type: "Feature",
          properties: Object.assign({}, feat.properties, { id: "" }),
          geometry: { type: "LineString", coordinates: line },
        });
      });
      return;
    }
    addFeatureAsLayer(feat);
  });
  refreshList();
  applyZoneVisibility();
}

/* --- server ---------------------------------------------------------- */

function setStatus(msg, kind) {
  const node = el("status");
  node.textContent = msg || "\u00a0";
  node.className = `status ${kind || ""}`;
}

function layersBounds(list) {
  // Circle.getBounds() needs a projected map (_map + _point). Before the first
  // setView that throws and aborts load, so derive bounds from lat/lon + radius.
  const bounds = L.latLngBounds([]);
  list.forEach((layer) => {
    if (layer instanceof L.Circle) {
      const c = layer.getLatLng();
      const r = layer.getRadius();
      const dLat = r / M_PER_DEG_LAT;
      const dLng = r / (M_PER_DEG_LAT * Math.max(Math.cos((c.lat * Math.PI) / 180), 1e-6));
      bounds.extend([c.lat - dLat, c.lng - dLng]);
      bounds.extend([c.lat + dLat, c.lng + dLng]);
      return;
    }
    if (layer.getLatLngs) {
      const raw = layer.getLatLngs();
      const ring = Array.isArray(raw[0]) ? raw.flat(1) : raw;
      ring.forEach((p) => bounds.extend(p));
      return;
    }
    if (layer.getLatLng) bounds.extend(layer.getLatLng());
  });
  return bounds;
}

async function loadState(airportKey) {
  const q = airportKey ? `?airport=${encodeURIComponent(airportKey)}` : "";
  const res = await fetch(`/api/state${q}`);
  const s = await res.json();

  state.airport = s.airport;
  state.runways = s.runways || [];
  el("path").textContent = s.path;

  const sel = el("airport");
  sel.textContent = "";
  (s.airports || []).forEach((a) => {
    const opt = document.createElement("option");
    opt.value = a.key;
    opt.textContent = a.icao ? `${a.name} (${a.icao})` : a.name;
    opt.selected = a.key === s.airport;
    sel.appendChild(opt);
  });

  const trig = el("trigger");
  if (!trig.options.length) {
    (s.triggers || []).forEach((t) => {
      const opt = document.createElement("option");
      opt.value = t;
      opt.textContent = TRIGGER_LABELS[t] || t;
      opt.selected = t === "in_position";
      trig.appendChild(opt);
    });
    // A step matches trigger names as plain strings, so an agency this tool has
    // never heard of only needs typing.
    const custom = document.createElement("option");
    custom.value = CUSTOM;
    custom.textContent = "Custom name…";
    trig.appendChild(custom);
  }

  const rwy = el("runway");
  rwy.textContent = "";
  ["", ...state.runways].forEach((r) => {
    const opt = document.createElement("option");
    opt.value = r;
    opt.textContent = r || "any / not runway specific";
    rwy.appendChild(opt);
  });

  setCalibrationPill(s.calibrated);

  // Give the map a centre before adding geometry. Vector layers (especially
  // circles) cannot answer getBounds() until the map has been projected once.
  const centre = Array.isArray(s.centre) && s.centre.length === 2 ? s.centre : [36.236, -115.034];
  map.setView(centre, 14);
  map.invalidateSize();

  loadGeoJSON(s.geojson || { features: [] }, { clear: true });
  state.dirty = false;
  applyZoneVisibility();

  map.invalidateSize();
  const bounds = layersBounds(layers());
  if (bounds.isValid()) map.fitBounds(bounds.pad(0.6), { maxZoom: 17 });
  requestAnimationFrame(() => {
    map.invalidateSize();
    if (mover.layer) mover.reseat();
    if (rotation.layer) rotation.reseat();
  });

  setStatus(layers().length ? "Loaded existing geometry." : "Nothing drawn for this field yet.", "");
}

async function save() {
  setStatus("Saving…", "");
  const res = await fetch("/api/save", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      airport: state.airport,
      geojson: toGeoJSON(),
      replace: true,
    }),
  });
  const out = await res.json();
  if (!out.ok) {
    setStatus(`Save failed: ${out.error}`, "err");
    return;
  }
  state.dirty = false;
  syncSaveButton();
  const bits = [`Saved ${out.zones} zone(s)`];
  if (out.runways.length) bits.push(`runways ${out.runways.join(", ")}`);
  bits.push(out.calibrated ? "field is calibrated" : "not calibrated yet");
  const warnings = out.warnings || [];
  setStatus(
    `${bits.join(" · ")}. ${warnings.length ? warnings.join("; ") : `Backup: ${out.backup}`}`,
    warnings.length ? "warn" : "ok"
  );
  setCalibrationPill(out.calibrated);
}

function setCalibrationPill(calibrated) {
  const pill = el("calib");
  pill.textContent = calibrated
    ? "Calibrated — auto clearances can use this field"
    : "Not calibrated yet — needs a centreline and an in-position area";
  pill.className = `pill ${calibrated ? "ok" : "no"}`;
}

/* --- reference overlay (view-only KML/KMZ) --------------------------- */

function setOverlayStatus(msg) {
  el("overlayStatus").textContent = msg || "\u00a0";
}

function clearOverlay() {
  overlayRoot.clearLayers();
  overlayHighlight.clearLayers();
  overlayState.id = null;
  overlayState.name = "";
  overlayState.groups.clear();
  overlayState.enabled.clear();
  overlayState.master = true;
  overlayState.tree = null;
  overlayState.index = [];
  overlayState.hitIndex = -1;
  overlayState.expanded.clear();
  if (overlayPulseTimer) {
    clearTimeout(overlayPulseTimer);
    overlayPulseTimer = null;
  }
  if (overlayLayerCtl) overlayLayerCtl.reset();
  setOverlayStatus("");
}

function applyOverlayVisibility() {
  const master = overlayState.master;
  overlayState.groups.forEach((group, folder) => {
    const on = master && overlayState.enabled.get(folder);
    if (on) {
      if (!overlayRoot.hasLayer(group)) overlayRoot.addLayer(group);
    } else if (overlayRoot.hasLayer(group)) {
      overlayRoot.removeLayer(group);
    }
  });
}

function folderLabel(folder) {
  const parts = String(folder || "").split("/").filter(Boolean);
  if (parts.length <= 2) return folder;
  return parts.slice(-2).join(" / ");
}

function geomKind(type) {
  if (type === "Point") return "point";
  if (type === "LineString" || type === "MultiLineString") return "line";
  if (type === "Polygon" || type === "MultiPolygon") return "area";
  return (type || "shape").toLowerCase();
}

function layerFocus(layer) {
  if (layer.getLatLng) return { center: layer.getLatLng(), bounds: null };
  if (layer.getBounds) {
    const bounds = layer.getBounds();
    if (bounds && bounds.isValid()) return { center: bounds.getCenter(), bounds };
  }
  return null;
}

function buildOverlayTree(folders) {
  const root = {
    name: "Reference",
    path: "",
    children: new Map(),
    leaf: null,
    count: 0,
    default_on: false,
  };
  (folders || []).forEach((f) => {
    const parts = String(f.id || "")
      .split("/")
      .map((p) => p.trim())
      .filter(Boolean);
    if (!parts.length) parts.push("(root)");
    let node = root;
    parts.forEach((part, i) => {
      if (!node.children.has(part)) {
        node.children.set(part, {
          name: part,
          path: parts.slice(0, i + 1).join("/"),
          children: new Map(),
          leaf: null,
          count: 0,
          default_on: false,
        });
      }
      node = node.children.get(part);
    });
    node.leaf = f.id;
    node.count = f.count || 0;
    node.default_on = !!f.default_on;
  });
  const rollup = (node) => {
    let count = node.leaf ? node.count : 0;
    let anyDefault = node.leaf ? node.default_on : false;
    node.children.forEach((child) => {
      rollup(child);
      count += child.count;
      anyDefault = anyDefault || child.default_on;
    });
    node.count = count;
    node.default_on = anyDefault;
  };
  rollup(root);
  return root;
}

function leafPathsUnder(node) {
  const out = [];
  if (node.leaf) out.push(node.leaf);
  node.children.forEach((child) => out.push(...leafPathsUnder(child)));
  return out;
}

function nodeCheckState(node) {
  const leaves = leafPathsUnder(node);
  if (!leaves.length) return { checked: false, indeterminate: false };
  let on = 0;
  leaves.forEach((path) => {
    if (overlayState.enabled.get(path)) on += 1;
  });
  return {
    checked: on === leaves.length,
    indeterminate: on > 0 && on < leaves.length,
  };
}

function setLeavesEnabled(node, on) {
  leafPathsUnder(node).forEach((path) => overlayState.enabled.set(path, on));
  applyOverlayVisibility();
  if (overlayLayerCtl) overlayLayerCtl.syncChecks();
}

function revealOverlayFolder(folder) {
  overlayState.master = true;
  overlayState.enabled.set(folder, true);
  const parts = String(folder || "").split("/");
  for (let i = 1; i <= parts.length; i += 1) {
    overlayState.expanded.add(parts.slice(0, i).join("/"));
  }
  applyOverlayVisibility();
  if (overlayLayerCtl) {
    overlayLayerCtl.open();
    overlayLayerCtl.rebuildTree();
  }
}

function pulseOverlayHit(layer) {
  overlayHighlight.clearLayers();
  if (overlayPulseTimer) clearTimeout(overlayPulseTimer);
  const focus = layerFocus(layer);
  if (!focus) return;
  const marker = L.circleMarker(focus.center, {
    radius: 10,
    color: "#fff",
    weight: 2,
    fillColor: "#f59e0b",
    fillOpacity: 0.55,
  }).addTo(overlayHighlight);
  let step = 0;
  const tick = setInterval(() => {
    step += 1;
    marker.setRadius(10 + (step % 2) * 6);
    marker.setStyle({ fillOpacity: step % 2 ? 0.25 : 0.6 });
    if (step > 8) clearInterval(tick);
  }, 180);
  overlayPulseTimer = setTimeout(() => {
    overlayHighlight.clearLayers();
    clearInterval(tick);
    overlayPulseTimer = null;
  }, 2200);
}

function goToOverlayHit(entry) {
  if (!entry || !entry.layer) return;
  revealOverlayFolder(entry.folder);
  const focus = layerFocus(entry.layer);
  if (focus && focus.bounds && focus.bounds.isValid()) {
    const sw = focus.bounds.getSouthWest();
    const ne = focus.bounds.getNorthEast();
    const tiny =
      Math.abs(ne.lat - sw.lat) < 0.0008 && Math.abs(ne.lng - sw.lng) < 0.0008;
    if (!tiny) {
      map.fitBounds(focus.bounds.pad(0.35), { maxZoom: 15 });
    } else {
      map.setView(focus.center, Math.max(map.getZoom(), 13));
    }
  } else if (focus) {
    map.setView(focus.center, Math.max(map.getZoom(), 13));
  }
  pulseOverlayHit(entry.layer);
}

function nodeMatchesQuery(node, q) {
  if (!q) return true;
  if (node.name.toLowerCase().includes(q) || node.path.toLowerCase().includes(q)) {
    return true;
  }
  if (
    node.leaf &&
    overlayState.index.some((e) => e.folder === node.leaf && e.nameLower.includes(q))
  ) {
    return true;
  }
  for (const child of node.children.values()) {
    if (nodeMatchesQuery(child, q)) return true;
  }
  return false;
}

const OverlayLayerControl = L.Control.extend({
  options: { position: "topright" },

  onAdd() {
    const root = L.DomUtil.create("div", "leaflet-bar overlay-layer-ctl");
    L.DomEvent.disableClickPropagation(root);
    L.DomEvent.disableScrollPropagation(root);

    const toggle = L.DomUtil.create("a", "olc-toggle", root);
    toggle.href = "#";
    toggle.title = "Reference layers";
    toggle.setAttribute("role", "button");
    toggle.textContent = "☰";
    L.DomEvent.on(toggle, "click", L.DomEvent.stop).on(toggle, "click", () => {
      this.open();
    });

    const panel = L.DomUtil.create("div", "olc-panel", root);
    const head = L.DomUtil.create("div", "olc-head", panel);
    const title = L.DomUtil.create("strong", "", head);
    title.textContent = "Layers";
    const close = L.DomUtil.create("button", "olc-close", head);
    close.type = "button";
    close.textContent = "✕";
    close.title = "Collapse";
    L.DomEvent.on(close, "click", L.DomEvent.stop).on(close, "click", () => {
      this.close();
    });

    const search = L.DomUtil.create("input", "olc-search", panel);
    search.type = "search";
    search.id = "overlaySearch";
    search.placeholder = "Search layers & points…";
    search.autocomplete = "off";
    search.disabled = true;
    L.DomEvent.on(search, "input", () => {
      clearTimeout(overlaySearchTimer);
      overlaySearchTimer = setTimeout(() => this.runSearch(), 120);
    });
    L.DomEvent.on(search, "keydown", (ev) => {
      const items = [...this._hits.querySelectorAll("li:not(.empty)")];
      if (!items.length) return;
      if (ev.key === "ArrowDown" || ev.key === "ArrowUp") {
        L.DomEvent.stop(ev);
        const dir = ev.key === "ArrowDown" ? 1 : -1;
        overlayState.hitIndex = Math.max(
          0,
          Math.min(items.length - 1, overlayState.hitIndex + dir)
        );
        items.forEach((n, i) => n.classList.toggle("active", i === overlayState.hitIndex));
        items[overlayState.hitIndex].scrollIntoView({ block: "nearest" });
      } else if (ev.key === "Enter" && overlayState.hitIndex >= 0) {
        L.DomEvent.stop(ev);
        items[overlayState.hitIndex].click();
      }
    });

    const body = L.DomUtil.create("div", "olc-body", panel);
    this._root = root;
    this._toggle = toggle;
    this._panel = panel;
    this._title = title;
    this._search = search;
    this._body = body;
    this._treeBox = null;
    this._hits = null;
    this.reset();
    return root;
  },

  open() {
    L.DomUtil.addClass(this._root, "open");
  },

  close() {
    L.DomUtil.removeClass(this._root, "open");
  },

  reset() {
    this._title.textContent = "Layers";
    this._search.value = "";
    this._search.disabled = true;
    this._body.textContent = "";
    const empty = L.DomUtil.create("div", "olc-empty", this._body);
    empty.textContent = "Load a reference pack from the sidebar to populate layers.";
    this._treeBox = null;
    this._hits = null;
    this.close();
  },

  setPack(meta, folders) {
    overlayState.tree = buildOverlayTree(folders);
    overlayState.name = meta.name || meta.id || "Reference";
    overlayState.master = true;
    overlayState.expanded.clear();
    // Open top-level branches that have something on by default (Airspace, Targets…).
    overlayState.tree.children.forEach((child) => {
      if (child.default_on) overlayState.expanded.add(child.path);
    });
    this._title.textContent = overlayState.name;
    this._search.disabled = false;
    this._search.value = "";
    this.rebuildTree();
    this.open();
  },

  rebuildTree() {
    const q = (this._search.value || "").trim().toLowerCase();
    this._body.textContent = "";

    if (!overlayState.tree) {
      this.reset();
      return;
    }

    const masterRow = L.DomUtil.create("div", "olc-row", this._body);
    const masterTwist = L.DomUtil.create("button", "olc-twist leaf", masterRow);
    masterTwist.type = "button";
    masterTwist.textContent = "•";
    const masterCb = L.DomUtil.create("input", "", masterRow);
    masterCb.type = "checkbox";
    masterCb.checked = overlayState.master;
    masterCb.title = "Show / hide all reference layers";
    L.DomEvent.on(masterCb, "change", () => {
      overlayState.master = masterCb.checked;
      applyOverlayVisibility();
    });
    const masterLabel = L.DomUtil.create("span", "olc-label", masterRow);
    masterLabel.textContent = "Show reference layers";
    masterLabel.addEventListener("click", () => {
      masterCb.checked = !masterCb.checked;
      overlayState.master = masterCb.checked;
      applyOverlayVisibility();
    });

    const section = L.DomUtil.create("div", "olc-section", this._body);
    section.textContent = "Folders";

    this._treeBox = L.DomUtil.create("ul", "olc-tree", this._body);
    const kids = [...overlayState.tree.children.values()].sort((a, b) =>
      a.name.localeCompare(b.name)
    );
    kids.forEach((child) => this._renderNode(this._treeBox, child, q));

    const hitSection = L.DomUtil.create("div", "olc-section", this._body);
    hitSection.textContent = q ? "Matching places" : "Places";
    hitSection.style.display = q ? "" : "none";
    this._hits = L.DomUtil.create("ul", "olc-hits", this._body);
    if (q) this._fillHits(q);
  },

  _renderNode(parentUl, node, q) {
    if (q && !nodeMatchesQuery(node, q)) return;

    const li = L.DomUtil.create("li", "olc-node", parentUl);
    const row = L.DomUtil.create("div", "olc-row", li);
    const hasKids = node.children.size > 0;
    const expanded = q ? true : overlayState.expanded.has(node.path);

    const twist = L.DomUtil.create("button", `olc-twist${hasKids ? "" : " leaf"}`, row);
    twist.type = "button";
    twist.textContent = hasKids ? (expanded ? "▼" : "▶") : "•";
    if (hasKids) {
      L.DomEvent.on(twist, "click", L.DomEvent.stop).on(twist, "click", () => {
        if (overlayState.expanded.has(node.path)) overlayState.expanded.delete(node.path);
        else overlayState.expanded.add(node.path);
        this.rebuildTree();
      });
    }

    const cb = L.DomUtil.create("input", "", row);
    cb.type = "checkbox";
    const state = nodeCheckState(node);
    cb.checked = state.checked;
    cb.indeterminate = state.indeterminate;
    cb.dataset.path = node.path;
    L.DomEvent.on(cb, "change", () => {
      setLeavesEnabled(node, cb.checked);
    });

    const label = L.DomUtil.create("span", "olc-label", row);
    label.textContent = node.name;
    label.title = node.path || node.name;
    label.addEventListener("click", () => {
      if (hasKids) {
        if (overlayState.expanded.has(node.path)) overlayState.expanded.delete(node.path);
        else overlayState.expanded.add(node.path);
        this.rebuildTree();
      } else {
        cb.checked = !cb.checked;
        setLeavesEnabled(node, cb.checked);
      }
    });

    if (node.count) {
      const count = L.DomUtil.create("span", "olc-count", row);
      count.textContent = String(node.count);
    }

    if (hasKids && expanded) {
      const childUl = L.DomUtil.create("ul", "", li);
      const kids = [...node.children.values()].sort((a, b) => a.name.localeCompare(b.name));
      kids.forEach((child) => this._renderNode(childUl, child, q));
    }
  },

  syncChecks() {
    if (!this._body) return;
    this._body.querySelectorAll('input[type="checkbox"][data-path]').forEach((cb) => {
      const path = cb.dataset.path;
      const find = (node) => {
        if (node.path === path) return node;
        for (const child of node.children.values()) {
          const hit = find(child);
          if (hit) return hit;
        }
        return null;
      };
      const node = overlayState.tree && find(overlayState.tree);
      if (!node) return;
      const state = nodeCheckState(node);
      cb.checked = state.checked;
      cb.indeterminate = state.indeterminate;
    });
    const master = this._body.querySelector(".olc-row input[type='checkbox']:not([data-path])");
    if (master) master.checked = overlayState.master;
  },

  runSearch() {
    this.rebuildTree();
  },

  _fillHits(q) {
    overlayState.hitIndex = -1;
    this._hits.textContent = "";
    const hits = [];
    for (const entry of overlayState.index) {
      if (!entry.nameLower.includes(q) && !entry.folder.toLowerCase().includes(q)) {
        continue;
      }
      hits.push(entry);
      if (hits.length >= OVERLAY_HIT_LIMIT) break;
    }
    if (!hits.length) {
      const li = L.DomUtil.create("li", "empty", this._hits);
      li.textContent = "No matching places.";
      return;
    }
    hits.forEach((entry, i) => {
      const li = L.DomUtil.create("li", "", this._hits);
      const title = L.DomUtil.create("b", "", li);
      title.textContent = entry.name || "(unnamed)";
      const meta = L.DomUtil.create("small", "", li);
      meta.textContent = `${geomKind(entry.type)} · ${folderLabel(entry.folder)}`;
      meta.title = entry.folder;
      li.addEventListener("click", () => {
        this._hits.querySelectorAll("li").forEach((n) => n.classList.remove("active"));
        li.classList.add("active");
        overlayState.hitIndex = i;
        goToOverlayHit(entry);
      });
    });
    const more = overlayState.index.filter(
      (e) => e.nameLower.includes(q) || e.folder.toLowerCase().includes(q)
    ).length;
    if (more > hits.length) {
      const li = L.DomUtil.create("li", "empty", this._hits);
      li.textContent = `Showing ${hits.length} of ${more} — refine the search.`;
    }
  },
});

const overlayLayerCtl = new OverlayLayerControl().addTo(map);

function overlayStyle(feature) {
  const t = (feature.geometry && feature.geometry.type) || "";
  if (t === "LineString" || t === "MultiLineString") {
    return {
      color: "#38bdf8",
      weight: 1.2,
      opacity: 0.75,
      pane: "refOverlay",
      renderer: overlayCanvas,
    };
  }
  return {
    color: "#fbbf24",
    weight: 1,
    opacity: 0.85,
    fillColor: "#fbbf24",
    fillOpacity: 0.07,
    pane: "refOverlay",
    renderer: overlayCanvas,
  };
}

function overlayPoint(feature, latlng) {
  return L.circleMarker(latlng, {
    radius: 3,
    color: "#fde68a",
    weight: 1,
    fillColor: "#fbbf24",
    fillOpacity: 0.85,
    pane: "refOverlay",
    renderer: overlayCanvas,
  });
}

async function refreshOverlayList(selectId) {
  const sel = el("overlayPick");
  const previous = selectId || sel.value;
  let list = [];
  try {
    const res = await fetch("/api/overlays");
    if (!res.ok) {
      throw new Error(
        res.status === 404
          ? "This editor is an old copy — close it and reopen Draw zones / Open-Zone-Editor.cmd"
          : `overlays HTTP ${res.status}`
      );
    }
    const out = await res.json();
    if (!out.ok) throw new Error(out.error || "overlays failed");
    list = out.overlays || [];
  } catch (err) {
    sel.textContent = "";
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(packs unavailable — restart the editor)";
    sel.appendChild(opt);
    setOverlayStatus(String(err.message || err));
    return;
  }
  sel.textContent = "";
  if (!list.length) {
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(drop a .kmz into tools/overlays/ or Upload below)";
    sel.appendChild(opt);
    setOverlayStatus("No packs in tools/overlays/ yet.");
    return;
  }
  list.forEach((item) => {
    const opt = document.createElement("option");
    opt.value = item.id;
    const mb = (item.bytes / (1024 * 1024)).toFixed(1);
    opt.textContent = `${item.name} (${mb} MB${item.cached ? ", cached" : ""})`;
    sel.appendChild(opt);
  });
  if (previous && list.some((i) => i.id === previous)) sel.value = previous;
  else if (list.some((i) => /nttr/i.test(i.id))) {
    sel.value = list.find((i) => /nttr/i.test(i.id)).id;
  }
  setOverlayStatus(`${list.length} pack(s) ready — pick one and Show on map.`);
}

async function loadOverlayPack(fileId) {
  if (!fileId) {
    setOverlayStatus("Pick a pack first (or upload one).");
    return;
  }
  setOverlayStatus("Loading reference pack… (large KMZs take a few seconds)");
  el("overlayLoad").disabled = true;
  try {
    const metaRes = await fetch(`/api/overlay?file=${encodeURIComponent(fileId)}`);
    const meta = await metaRes.json();
    if (!meta.ok) {
      setOverlayStatus(`Failed: ${meta.error || metaRes.status}`);
      return;
    }
    const geoRes = await fetch(meta.geojson_url || `/api/overlay/geojson?file=${encodeURIComponent(fileId)}`);
    if (!geoRes.ok) {
      setOverlayStatus(`Failed to load geometry (${geoRes.status})`);
      return;
    }
    const collection = await geoRes.json();
    const features = collection.features || [];

    clearOverlay();
    overlayState.id = meta.id;

    const byFolder = new Map();
    features.forEach((feat) => {
      const folder = (feat.properties && feat.properties.folder) || "(root)";
      if (!byFolder.has(folder)) byFolder.set(folder, []);
      byFolder.get(folder).push(feat);
    });

    const folderMeta = new Map(
      (meta.folders || []).map((f) => [f.id, f])
    );
    const index = [];
    byFolder.forEach((feats, folder) => {
      const group = L.geoJSON(
        { type: "FeatureCollection", features: feats },
        {
          style: overlayStyle,
          pointToLayer: overlayPoint,
          onEachFeature(feature, layer) {
            const name = (feature.properties && feature.properties.name) || "";
            if (name) layer.bindTooltip(name, { sticky: true, opacity: 0.9 });
            index.push({
              name,
              nameLower: name.toLowerCase(),
              folder,
              type: (feature.geometry && feature.geometry.type) || "",
              layer,
            });
          },
        }
      );
      overlayState.groups.set(folder, group);
      const info = folderMeta.get(folder);
      overlayState.enabled.set(folder, info ? !!info.default_on : false);
    });
    index.sort((a, b) => a.nameLower.localeCompare(b.nameLower));
    overlayState.index = index;

    overlayLayerCtl.setPack(meta, meta.folders || []);
    applyOverlayVisibility();
    setOverlayStatus(
      `${meta.name}: ${meta.count} shapes · use the Layers panel (top-right) to toggle folders / search`
    );
  } catch (err) {
    setOverlayStatus(String(err));
  } finally {
    el("overlayLoad").disabled = false;
  }
}

async function uploadOverlay(file) {
  setOverlayStatus(`Uploading ${file.name}…`);
  const res = await fetch(`/api/overlay?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    body: await file.arrayBuffer(),
  });
  const meta = await res.json();
  if (!meta.ok) {
    setOverlayStatus(`Upload failed: ${meta.error || res.status}`);
    return;
  }
  await refreshOverlayList(meta.id);
  await loadOverlayPack(meta.id);
}

/* --- live tracks ----------------------------------------------------- */

let trackTimer = null;

function offsetLatLng(lat, lon, headingDeg, metres) {
  const rad = (headingDeg * Math.PI) / 180;
  const dLat = (metres * Math.cos(rad)) / 110540;
  const dLon =
    (metres * Math.sin(rad)) / (111320 * Math.cos((lat * Math.PI) / 180) || 1);
  return [lat + dLat, lon + dLon];
}

async function pollTracks() {
  if (!el("tracks").checked) return;
  try {
    const res = await fetch(`/api/tracks?airport=${encodeURIComponent(state.airport)}`);
    const out = await res.json();
    trackLayer.clearLayers();
    if (!out.ok) {
      el("verdict").textContent = out.reason || "no feed";
      return;
    }
    let own = null;
    (out.tracks || []).forEach((t) => {
      const colour = t.own ? "#ff4d4d" : "#ffffff";
      L.circleMarker([t.lat, t.lon], {
        radius: t.own ? 6 : 4,
        color: colour,
        weight: 2,
        fillColor: colour,
        fillOpacity: t.own ? 0.9 : 0.5,
      })
        .bindTooltip(
          [
            t.label,
            t.agl_ft != null
              ? `${Math.round(t.agl_ft)} ft AGL`
              : t.alt_ft != null
              ? `${Math.round(t.alt_ft)} ft MSL`
              : "",
            t.zone ? `in ${t.zone}` : "",
          ]
            .filter(Boolean)
            .join(" · "),
          { direction: "right", className: "trk-label" }
        )
        .addTo(trackLayer);
      if (t.heading != null) {
        L.polyline([[t.lat, t.lon], offsetLatLng(t.lat, t.lon, t.heading, 200)], {
          color: colour,
          weight: 1.5,
          opacity: 0.8,
        }).addTo(trackLayer);
      }
      if (t.own) own = t;
    });
    el("verdict").textContent = own
      ? `${own.label} — ${own.zone ? `in ${own.zone}` : "in no zone"}`
      : `${out.count} track(s), none matched to your flight`;
  } catch (err) {
    el("verdict").textContent = String(err);
  }
}

/* --- wiring ---------------------------------------------------------- */

el("airport").addEventListener("change", async (ev) => {
  if (state.dirty && !confirm("Discard unsaved changes?")) {
    ev.target.value = state.airport;
    return;
  }
  await loadState(ev.target.value);
});

["trigger", "runway"].forEach((id) =>
  el(id).addEventListener("change", onFormEdit)
);
["customTrigger", "name", "minAlt", "maxAlt"].forEach((id) =>
  el(id).addEventListener("input", onFormEdit)
);

el("deselect").addEventListener("click", () => select(null, { zoom: false }));
el("newZone").addEventListener("click", startNewZone);
el("deleteZone").addEventListener("click", deleteSelectedZone);
el("lockZone").addEventListener("click", lockSelectedZone);
el("unlockZone").addEventListener("click", unlockSelectedZone);
el("rotateLeft").addEventListener("click", () => rotation.nudge(-15));
el("rotateRight").addEventListener("click", () => rotation.nudge(15));
el("copyZone").addEventListener("click", copySelected);
el("pasteZone").addEventListener("click", () => {
  pasteAsNew().catch((err) => setStatus(String(err), "err"));
});

el("save").addEventListener("click", save);

el("export").addEventListener("click", () => {
  const blob = new Blob([JSON.stringify(toGeoJSON(), null, 2)], {
    type: "application/geo+json",
  });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = `${state.airport || "zones"}.geojson`;
  a.click();
  URL.revokeObjectURL(a.href);
});

el("import").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  ev.target.value = "";
  setStatus(`Reading ${file.name}…`, "");
  try {
    const res = await fetch(`/api/parse?filename=${encodeURIComponent(file.name)}`, {
      method: "POST",
      body: await file.arrayBuffer(),
    });
    const out = await res.json();
    if (!out.ok) {
      setStatus(`Import failed: ${out.error}`, "err");
      return;
    }
    loadGeoJSON(out.geojson, { clear: false });
    markDirty();
    const warn = out.warnings && out.warnings.length ? ` ${out.warnings.join("; ")}` : "";
    setStatus(
      `Imported ${out.geojson.features.length} shape(s) from ${file.name}.${warn}`,
      warn ? "warn" : "ok"
    );
  } catch (err) {
    setStatus(String(err), "err");
  }
});

el("tracks").addEventListener("change", (ev) => {
  clearInterval(trackTimer);
  trackLayer.clearLayers();
  el("verdict").textContent = "\u00a0";
  if (ev.target.checked) {
    pollTracks();
    trackTimer = setInterval(pollTracks, TRACK_POLL_MS);
  }
});

el("showAllZones").addEventListener("click", () => setAllZonesHidden(false));
el("hideAllZones").addEventListener("click", () => setAllZonesHidden(true));

el("overlayLoad").addEventListener("click", () => {
  loadOverlayPack(el("overlayPick").value).catch((err) => setOverlayStatus(String(err)));
});
el("overlayClear").addEventListener("click", clearOverlay);
el("overlayUpload").addEventListener("change", async (ev) => {
  const file = ev.target.files && ev.target.files[0];
  if (!file) return;
  ev.target.value = "";
  try {
    await uploadOverlay(file);
  } catch (err) {
    setOverlayStatus(String(err));
  }
});

window.addEventListener("keydown", (ev) => {
  const tag = (ev.target && ev.target.tagName) || "";
  const typing = tag === "INPUT" || tag === "SELECT" || tag === "TEXTAREA";

  if (!typing && !ev.ctrlKey && !ev.metaKey && !ev.altKey) {
    if (ev.key === "h" || ev.key === "H") {
      ev.preventDefault();
      const layer = findLayer(state.selected);
      if (layer) setZoneHidden(layer, !layer.zoneHidden);
      else setAllZonesHidden(layers().every((l) => !l.zoneHidden));
      return;
    }
    if ((ev.key === "Delete" || ev.key === "Backspace") && state.selected) {
      ev.preventDefault();
      deleteSelectedZone();
      return;
    }
    if (ev.key === "n" || ev.key === "N") {
      ev.preventDefault();
      startNewZone();
      return;
    }
  }

  if (!(ev.ctrlKey || ev.metaKey) || typing) return;
  if (ev.key === "c" || ev.key === "C") {
    if (!state.selected) return;
    ev.preventDefault();
    copySelected();
  } else if (ev.key === "v" || ev.key === "V") {
    ev.preventDefault();
    pasteAsNew().catch((err) => setStatus(String(err), "err"));
  }
});

window.addEventListener("beforeunload", (ev) => {
  if (!state.dirty) return;
  ev.preventDefault();
  ev.returnValue = "";
});

const params = new URLSearchParams(location.search);
syncSaveButton();
refreshOverlayList().catch(() => {});
loadState(params.get("airport") || "").catch((err) => {
  console.error(err);
  setStatus(String(err), "err");
});
