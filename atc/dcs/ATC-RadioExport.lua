--[[
  ATC External Audio — radio frequency mirror + ownship aircraft snapshot.

  Install via Setup → Controls → "Install DCS radio export…" or
  Install-DCS-Radio-Export.cmd (idempotent). Do not hand-edit Export.lua.

  Performance (intentional):
    * Almost every frame: one time compare, then return (no device I/O).
    * ~1 Hz while in a unit: read only cached radio device ids, write
      radios.json only when freqs/unit change.
    * ~5 Hz while in a unit: small ownship snapshot (mech + named cockpit
      args) for Virtual Crew Chief. Write aircraft.json on change, or at
      least once a second so the app does not treat the file as stale.
    * Menus / no unit: keepalive write at most every 5 s.

  Writes Saved Games\DCS*\ATC-ExternalAudio\radios.json
  Writes Saved Games\DCS*\ATC-ExternalAudio\aircraft.json
  Frequencies are in Hz (same units as SRS).
]]

local ATC = ATC_ExternalAudio_Radio or {}
ATC_ExternalAudio_Radio = ATC

ATC._prevAfter = LuaExportAfterNextFrame
ATC._prevStart = LuaExportStart
ATC._prevStop = LuaExportStop

ATC._nextWrite = 0
ATC._interval = 1.0          -- 1 Hz is enough for the gate (stale window ~2–3 s)
ATC._emptyInterval = 5.0     -- while not in a unit, keepalive even rarer
ATC._maxDeviceId = 40        -- radios live well below this on clickable modules
ATC._deviceIds = nil         -- cached ids that returned a real freq for this unit
ATC._cachedUnit = ""
ATC._lastFingerprint = nil
ATC._dirReady = false
ATC._outPath = nil
ATC._acPath = nil
ATC._nextAc = 0
ATC._acInterval = 0.2        -- 5 Hz snapshot; cheap vs Tacview
ATC._lastAcFingerprint = nil
ATC._lastAcWrite = 0
ATC._acKeepalive = 1.0

-- F-16C args used by Virtual Crew Chief (Mission1.001 / Bogey Dope flow).
ATC._argIds = {
  94, 95, 356, 357, 363, 425, 440, 449, 510, 523, 526, 528,
  542, 555, 556, 561, 563, 564, 565, 566, 579, 585, 600, 607,
  693, 727, 736, 737, 780, 781, 782,
}

local function ensure_dir()
  if ATC._dirReady and ATC._outPath and ATC._acPath then
    return true
  end
  local lfs = require("lfs")
  local dir = lfs.writedir() .. "ATC-ExternalAudio"
  local attr = lfs.attributes(dir)
  if not (attr and attr.mode == "directory") then
    if lfs.mkdir(dir) ~= true then
      return false
    end
  end
  ATC._outPath = dir .. "\\radios.json"
  ATC._acPath = dir .. "\\aircraft.json"
  ATC._dirReady = true
  return true
end

local function json_escape(s)
  s = tostring(s or "")
  return s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
end

local function num(v, fallback)
  if type(v) == "number" then
    return v
  end
  if type(v) == "table" then
    if type(v.value) == "number" then
      return v.value
    end
    if type(v[1]) == "number" then
      return v[1]
    end
  end
  return fallback or 0
end

local function pair(v)
  if type(v) ~= "table" then
    local n = num(v, 0)
    return n, n
  end
  local left = num(v.left, num(v[1], 0))
  local right = num(v.right, num(v[2], left))
  return left, right
end

local function fingerprint(unit_name, radios)
  local parts = { unit_name or "" }
  for i = 1, #radios do
    local r = radios[i]
    parts[#parts + 1] = string.format(
      "%s:%.0f:%.0f:%d",
      r.name or "",
      r.freq or 0,
      r.secFreq or 0,
      r.modulation or 0
    )
  end
  return table.concat(parts, "|")
end

local function write_radios(unit_name, radios)
  if not ensure_dir() then
    return
  end
  local path = ATC._outPath
  local fp = fingerprint(unit_name, radios)
  if fp == ATC._lastFingerprint then
    return
  end
  ATC._lastFingerprint = fp

  local parts = {
    string.format('{"t":%d,"unit":"%s","radios":[', os.time(), json_escape(unit_name)),
  }
  for i = 1, #radios do
    if i > 1 then
      parts[#parts + 1] = ","
    end
    local r = radios[i]
    parts[#parts + 1] = string.format(
      '{"name":"%s","freq":%.0f,"secFreq":%.0f,"modulation":%d}',
      json_escape(r.name),
      r.freq or 0,
      r.secFreq or 0,
      r.modulation or 0
    )
  end
  parts[#parts + 1] = "]}\n"

  local f = io.open(path, "w")
  if not f then
    return
  end
  f:write(table.concat(parts))
  f:close()
end

local function read_arg(id)
  local ok, device = pcall(GetDevice, 0)
  if ok and device ~= nil and type(device) ~= "number" and device.get_argument_value then
    local ok_v, v = pcall(function()
      return device:get_argument_value(id)
    end)
    if ok_v and type(v) == "number" then
      return v
    end
  end
  if LoGetAircraftDrawArgumentValue then
    local ok_d, d = pcall(LoGetAircraftDrawArgumentValue, id)
    if ok_d and type(d) == "number" then
      return d
    end
  end
  return nil
end

local function write_aircraft(unit_name)
  if not ensure_dir() then
    return
  end
  local agl = 999
  local ias = 0
  if LoGetAltitudeAboveGroundLevel then
    local ok, v = pcall(LoGetAltitudeAboveGroundLevel)
    if ok and type(v) == "number" then
      agl = v
    end
  end
  if LoGetIndicatedAirSpeed then
    local ok, v = pcall(LoGetIndicatedAirSpeed)
    if ok and type(v) == "number" then
      ias = v
    end
  end

  local elev_l, elev_r, ail_l, ail_r, rud_l, rud_r = 0, 0, 0, 0, 0, 0
  local speedbrakes, gear, canopy, wheelbrakes = 0, 0, 0, 0
  local ok_m, mech = pcall(function()
    return LoGetMechInfo and LoGetMechInfo() or nil
  end)
  if ok_m and type(mech) == "table" then
    local cs = mech.controlsurfaces
    if type(cs) == "table" then
      elev_l, elev_r = pair(cs.elevator)
      ail_l, ail_r = pair(cs.eleron)
      if ail_l == 0 and ail_r == 0 then
        ail_l, ail_r = pair(cs.aileron)
      end
      rud_l, rud_r = pair(cs.rudder)
    end
    speedbrakes = num(mech.speedbrakes, 0)
    gear = num(mech.gear, 0)
    canopy = num(mech.canopy, 0)
    wheelbrakes = num(mech.wheelbrakes, 0)
  end

  local on_ground = false
  if unit_name ~= "" then
    on_ground = (agl < 3.0 and ias < 40.0) or (gear > 0.8 and agl < 8.0)
  end

  local arg_parts = {}
  local fp_parts = {
    unit_name or "",
    on_ground and "1" or "0",
    string.format("%.2f:%.1f:%.2f:%.2f:%.2f", agl, ias, elev_l, ail_l, rud_l),
  }
  if unit_name ~= "" then
    for i = 1, #ATC._argIds do
      local id = ATC._argIds[i]
      local v = read_arg(id)
      if type(v) == "number" then
        arg_parts[#arg_parts + 1] = string.format('"%d":%.3f', id, v)
        fp_parts[#fp_parts + 1] = string.format("%d:%.2f", id, v)
      end
    end
  end

  local fp = table.concat(fp_parts, "|")
  local now = LoGetModelTime and LoGetModelTime() or os.clock()
  local due = (now - (ATC._lastAcWrite or 0)) >= ATC._acKeepalive
  if fp == ATC._lastAcFingerprint and not due then
    return
  end
  ATC._lastAcFingerprint = fp
  ATC._lastAcWrite = now

  local body = string.format(
    '{"t":%d,"unit":"%s","on_ground":%s,"agl_m":%.2f,"ias_mps":%.2f,'
      .. '"mech":{"elevator":[%.3f,%.3f],"aileron":[%.3f,%.3f],'
      .. '"rudder":[%.3f,%.3f],"speedbrakes":%.3f,"gear":%.3f,'
      .. '"canopy":%.3f,"wheelbrakes":%.3f},"args":{%s}}\n',
    os.time(),
    json_escape(unit_name),
    on_ground and "true" or "false",
    agl,
    ias,
    elev_l,
    elev_r,
    ail_l,
    ail_r,
    rud_l,
    rud_r,
    speedbrakes,
    gear,
    canopy,
    wheelbrakes,
    table.concat(arg_parts, ",")
  )

  local f = io.open(ATC._acPath, "w")
  if not f then
    return
  end
  f:write(body)
  f:close()
end

local function read_device_freq(id)
  local ok, device = pcall(GetDevice, id)
  if not ok or device == nil or type(device) == "number" then
    return nil
  end
  local ok_freq, freq = pcall(function()
    return device:get_frequency()
  end)
  if not ok_freq or type(freq) ~= "number" or freq < 1000000 then
    return nil
  end
  local mod = 0
  local ok_mod, m = pcall(function()
    if device.get_modulation then
      return device:get_modulation()
    end
    return 0
  end)
  if ok_mod and type(m) == "number" then
    mod = m
  end
  return {
    name = "Device " .. tostring(id),
    freq = freq,
    secFreq = 0,
    modulation = mod,
  }
end

local function discover_device_ids()
  local ids = {}
  for id = 0, ATC._maxDeviceId do
    if read_device_freq(id) then
      ids[#ids + 1] = id
    end
  end
  return ids
end

local function scan_radios(unit_name)
  if unit_name ~= ATC._cachedUnit then
    ATC._cachedUnit = unit_name
    ATC._deviceIds = nil
    ATC._lastFingerprint = nil
  end

  if not ATC._deviceIds then
    ATC._deviceIds = discover_device_ids()
  end

  local radios = {}
  local alive = false
  for i = 1, #ATC._deviceIds do
    local entry = read_device_freq(ATC._deviceIds[i])
    if entry then
      alive = true
      radios[#radios + 1] = entry
    end
  end

  -- Aircraft change / cold start: rediscover once if cache went stale
  if not alive then
    ATC._deviceIds = discover_device_ids()
    for i = 1, #ATC._deviceIds do
      local entry = read_device_freq(ATC._deviceIds[i])
      if entry then
        radios[#radios + 1] = entry
      end
    end
  end
  return radios
end

function LuaExportStart()
  if ATC._prevStart then
    pcall(ATC._prevStart)
  end
  ATC._dirReady = false
  ATC._outPath = nil
  ATC._acPath = nil
  ATC._deviceIds = nil
  ATC._cachedUnit = ""
  ATC._lastFingerprint = nil
  ATC._lastAcFingerprint = nil
  ATC._lastAcWrite = 0
  ATC._nextWrite = 0
  ATC._nextAc = 0
end

function LuaExportStop()
  if ATC._prevStop then
    pcall(ATC._prevStop)
  end
end

function LuaExportAfterNextFrame()
  if ATC._prevAfter then
    pcall(ATC._prevAfter)
  end

  -- Fast path: nearly every frame exits here (one compare).
  local now = LoGetModelTime and LoGetModelTime() or os.clock()
  local ac_due = now >= (ATC._nextAc or 0)
  local radio_due = now >= (ATC._nextWrite or 0)
  if not ac_due and not radio_due then
    return
  end

  local unit = ""
  local ok, data = pcall(LoGetSelfData)
  if ok and data and data.Name then
    unit = tostring(data.Name)
  end

  if ac_due then
    ATC._nextAc = now + ATC._acInterval
    pcall(write_aircraft, unit)
  end

  if not radio_due then
    return
  end

  if unit == "" then
    ATC._nextWrite = now + ATC._emptyInterval
    ATC._nextAc = now + ATC._emptyInterval
    ATC._deviceIds = nil
    ATC._cachedUnit = ""
    -- Keepalive so ATC knows export is running but not in a jet
    pcall(write_radios, "", {})
    return
  end

  ATC._nextWrite = now + ATC._interval
  local radios = {}
  local ok_scan, scanned = pcall(scan_radios, unit)
  if ok_scan and type(scanned) == "table" then
    radios = scanned
  end
  pcall(write_radios, unit, radios)
end
