-- Coords plugin: mission environment
--
-- Only two of the six formats need the mission at all. MGRS and the three
-- lat/long forms are pure maths the bot does on its own; DCS metric X/Z and
-- terrain altitude depend on the loaded terrain's projection and heightmap, and
-- nothing outside the mission can supply those.

if jsh_coords then return end
jsh_coords = {}
local JC = jsh_coords

-- Bind the bot's table explicitly, as DCSServerBot's own mission scripts do.
-- It is a global by the time this runs, but relying on that turns a missing
-- DCSServerBot.lua into a silent no-op that looks exactly like a timeout.
local base = _G
dcsbot = dcsbot or base.dcsbot

JC.debug = true

local function log(msg)
  if JC.debug then env.info("[jsh_coords] " .. msg) end
end

local function reply(requestId, payload)
  payload = payload or {}
  payload.command = "jshCoordsResult"
  payload.request_id = requestId
  if dcsbot and dcsbot.sendBotTable then
    dcsbot.sendBotTable(payload)
  elseif not JC.warnedNoBot then
    JC.warnedNoBot = true
    env.error("[jsh_coords] cannot reply: DCSServerBot.lua is not loaded in this "
              .. "mission, so every conversion will time out")
  end
end

-- Terrain height at a map point. land.getHeight wants {x=, y=} where its y is
-- the map's z -- a naming trap worth stating, because passing a Vec3 here
-- returns the height at the wrong place rather than an error.
local function heightAt(x, z)
  local ok, height = pcall(land.getHeight, { x = x, y = z })
  if ok and type(height) == "number" then return height end
  return nil
end

---------------------------------------------------------------- CONVERSIONS

--- Latitude/longitude -> map metres, plus the terrain height there.
function JC.fromLatLon(requestId, lat, lon)
  local ok, x, z = pcall(coord.LLtoLO, lat, lon)
  if not ok then
    return reply(requestId, { ok = false, error = "LLtoLO failed: " .. tostring(x) })
  end
  -- coord.LLtoLO returns a Vec3 in some builds and three numbers in others.
  if type(x) == "table" then
    x, z = x.x, x.z
  end
  if type(x) ~= "number" or type(z) ~= "number" then
    return reply(requestId, { ok = false, error = "LLtoLO gave no usable coordinates" })
  end
  reply(requestId, { ok = true, x = x, z = z, altitude = heightAt(x, z),
                     theatre = env.mission and env.mission.theatre or nil })
end

--- Map metres -> latitude/longitude, plus the terrain height there.
function JC.fromMetric(requestId, x, z)
  local ok, lat, lon = pcall(coord.LOtoLL, { x = x, y = 0, z = z })
  if not ok then
    return reply(requestId, { ok = false, error = "LOtoLL failed: " .. tostring(lat) })
  end
  if type(lat) ~= "number" or type(lon) ~= "number" then
    return reply(requestId, { ok = false, error = "LOtoLL gave no usable coordinates" })
  end
  reply(requestId, { ok = true, lat = lat, lon = lon, altitude = heightAt(x, z),
                     theatre = env.mission and env.mission.theatre or nil })
end

log("mission environment loaded")
