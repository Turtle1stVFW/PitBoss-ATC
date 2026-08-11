--[[
  ATC External Audio — lightweight radio frequency mirror for the freq gate.

  Install via Setup → Controls → "Install DCS radio export…" or
  Install-DCS-Radio-Export.cmd (idempotent). Do not hand-edit Export.lua.

  Performance (intentional):
    * Almost every frame: one time compare, then return (no device I/O).
    * ~1 Hz while in a unit: read only cached radio device ids, write JSON
      only when freqs/unit change.
    * No work in the menu / spectator (empty unit) beyond the time check,
      except a rare empty-file write so the ATC app knows export is alive.

  Writes Saved Games\DCS*\ATC-ExternalAudio\radios.json
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

local function ensure_out_path()
  if ATC._dirReady and ATC._outPath then
    return ATC._outPath
  end
  local lfs = require("lfs")
  local dir = lfs.writedir() .. "ATC-ExternalAudio"
  local attr = lfs.attributes(dir)
  if not (attr and attr.mode == "directory") then
    if lfs.mkdir(dir) ~= true then
      return nil
    end
  end
  ATC._outPath = dir .. "\\radios.json"
  ATC._dirReady = true
  return ATC._outPath
end

local function json_escape(s)
  s = tostring(s or "")
  return s:gsub("\\", "\\\\"):gsub('"', '\\"'):gsub("\n", "\\n"):gsub("\r", "\\r")
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
  local path = ensure_out_path()
  if not path then
    return
  end
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
  ATC._deviceIds = nil
  ATC._cachedUnit = ""
  ATC._lastFingerprint = nil
  ATC._nextWrite = 0
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
  if now < (ATC._nextWrite or 0) then
    return
  end

  local unit = ""
  local ok, data = pcall(LoGetSelfData)
  if ok and data and data.Name then
    unit = tostring(data.Name)
  end

  if unit == "" then
    ATC._nextWrite = now + ATC._emptyInterval
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
