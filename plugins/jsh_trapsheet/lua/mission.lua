-- jsh_trapsheet: samples a carrier approach so the bot can draw a trapsheet.
--
-- MOOSE's AIRBOSS does this job for FunkMan. This does the same sampling with
-- nothing but the stock mission environment, so no MOOSE is needed in the
-- .miz. It produces exactly the four arrays DCSServerBot's own
-- plugins/greenieboard/trapsheet.py plots: X, Z, Alt and AoA.
--
-- Injected over `do_script`, so it is subject to the 8192-byte datagram limit
-- (bot -> DCS). Keep it small. The samples travel the OTHER way, through
-- dcsbot.sendBotTable, which chunks at 65000 bytes and is reassembled by the
-- bot, so a full approach is never at risk of truncation.

jsh_trapsheet = jsh_trapsheet or {}
local JT = jsh_trapsheet

JT.cfg = JT.cfg or {
  rate      = 0.1,     -- seconds between samples
  range     = 2778,    -- start sampling within this many metres (1.5 NM)
  ceiling   = 1500,    -- ... and below this altitude above the deck, in feet
  maxpoints = 600,     -- hard cap per pass, so a go-around cannot grow forever
  -- Deck type names to sample, matched as PREFIXES. Only Super Carrier decks
  -- raise S_EVENT_LANDING_QUALITY_MARK, so a pass anywhere else could never
  -- be graded. Filtering here rather than discarding later means a legacy
  -- deck costs no sampling work and sends no datagrams at all.
  decks     = { 'CVN_', 'CV_59', 'Forrestal' },
}
JT.tracks = JT.tracks or {}   -- [unitName] = {carrier=, name=, type=, pts={}}

local FEET = 3.28084

-- Decks we sample against: a carrier whose type matches one of cfg.decks.
--
-- Matched as a PREFIX, because the Super Carrier types are numbered per hull
-- (CVN_71, CVN_72, CVN_73, CVN_75) and an exact-name list would match none of
-- them on a real server.
local function wanted(typename)
  for _, p in ipairs(JT.cfg.decks or {}) do
    if typename:sub(1, #p) == p then return true end
  end
  return false
end

local function carriers()
  local found = {}
  for _, side in pairs({coalition.side.BLUE, coalition.side.RED}) do
    for _, grp in pairs(coalition.getGroups(side, Group.Category.SHIP) or {}) do
      for _, u in pairs(grp:getUnits() or {}) do
        if u:isExist() and u:hasAttribute('AircraftCarrier')
           and wanted(u:getTypeName()) then
          found[#found + 1] = u
        end
      end
    end
  end
  return found
end

-- Geometric angle of attack, in degrees.
--
-- There is no getAoA() in the mission environment. This is the angle between
-- the velocity vector and the airframe's longitudinal axis, measured in the
-- pitch plane -- which is what AoA is. getPosition() returns an orthonormal
-- basis: p.x is forward along the nose, p.y is up through the canopy.
--
-- NOTE this is geometric AoA in DEGREES. The Hornet's indexer units happen to
-- track degrees closely (on-speed 8.1), which is why the 7.4/8.8 reference
-- lines in the plot look right for it. Other airframes use their own units and
-- the reference band will not mean the same thing. See the README.
local function aoa(unit)
  local v = unit:getVelocity()
  local p = unit:getPosition()
  local speed = math.sqrt(v.x * v.x + v.y * v.y + v.z * v.z)
  if speed < 10 then return 0 end
  local fwd = v.x * p.x.x + v.y * p.x.y + v.z * p.x.z
  local up  = v.x * p.y.x + v.y * p.y.y + v.z * p.y.z
  return math.deg(math.atan2(-up, fwd))
end

-- Position of the aircraft in the carrier's own frame.
--
-- X is distance ASTERN in metres and Z is lateral offset, which is the frame
-- trapsheet.py expects: it adds a 20 m offset to each and then rotates by
-- -9 degrees for the angled deck. The signs here are the ones to check
-- against a real trap first -- see `axes` in the config if either is mirrored.
local function relative(unit, carrier)
  local u, c = unit:getPoint(), carrier:getPoint()
  local cp = carrier:getPosition()
  -- Ship's heading as a unit vector in the world x/z plane.
  local hx, hz = cp.x.x, cp.x.z
  local n = math.sqrt(hx * hx + hz * hz)
  if n < 1e-6 then return nil end
  hx, hz = hx / n, hz / n
  local dx, dz = u.x - c.x, u.z - c.z
  local fwd = dx * hx + dz * hz            -- + ahead of the ship
  local lat = -dx * hz + dz * hx           -- + to starboard
  return -fwd, lat, (u.y - c.y) * FEET     -- astern +, lateral, alt above deck
end

local function key(unit)
  local ok, name = pcall(function() return unit:getName() end)
  return ok and name or nil
end

-- One sampling tick.
function JT.tick()
  local decks = carriers()
  if #decks > 0 then
    for _, side in pairs({coalition.side.BLUE, coalition.side.RED}) do
      for _, grp in pairs(coalition.getGroups(side, Group.Category.AIRPLANE) or {}) do
        for _, u in pairs(grp:getUnits() or {}) do
          if u:isExist() and u:getPlayerName() then
            JT.sample(u, decks)
          end
        end
      end
    end
  end
  return timer.getTime() + JT.cfg.rate
end

function JT.sample(unit, decks)
  -- Nearest deck.
  local best, bestd
  local up = unit:getPoint()
  for _, c in pairs(decks) do
    local cp = c:getPoint()
    local d = math.sqrt((up.x - cp.x) ^ 2 + (up.z - cp.z) ^ 2)
    if not bestd or d < bestd then best, bestd = c, d end
  end
  if not best then return end

  local k = key(unit)
  if not k then return end
  local track = JT.tracks[k]

  local x, z, alt = relative(unit, best)
  if not x then return end

  local inside = bestd <= JT.cfg.range and alt <= JT.cfg.ceiling and x > 0

  if inside then
    if not track then
      track = { carrier = best:getName(), name = unit:getPlayerName(),
                utype = unit:getTypeName(), pts = {} }
      JT.tracks[k] = track
    end
    if #track.pts < JT.cfg.maxpoints then
      track.pts[#track.pts + 1] = {
        math.floor(x * 10) / 10,
        math.floor(z * 10) / 10,
        math.floor(alt * 10) / 10,
        math.floor(aoa(unit) * 100) / 100,
      }
    end
  elseif track then
    -- Left the window: either trapped, bolted or waved off. Either way the
    -- pass is over, so flush it and let the bot decide what it was.
    JT.flush(k)
  end
end

function JT.flush(k)
  local track = JT.tracks[k]
  JT.tracks[k] = nil
  if not track or #track.pts < 10 then return end   -- too short to plot
  dcsbot.sendBotTable({
    command  = 'jshTrapsheet',
    name     = track.name,
    unit_type = track.utype,
    carrier  = track.carrier,
    points   = track.pts,
  })
end

-- A landing or a crash ends a pass immediately, without waiting for the
-- aircraft to leave the capture window.
JT.handler = JT.handler or {}
function JT.handler:onEvent(e)
  if not e.initiator then return end
  if e.id == world.event.S_EVENT_LAND
     or e.id == world.event.S_EVENT_CRASH
     or e.id == world.event.S_EVENT_EJECTION then
    local k = key(e.initiator)
    if k and JT.tracks[k] then
      -- One tick of grace so the touchdown point itself is in the trace.
      timer.scheduleFunction(function() JT.flush(k) end, nil,
                             timer.getTime() + JT.cfg.rate)
    end
  end
end

if not JT.started then
  JT.started = true
  world.addEventHandler(JT.handler)
  timer.scheduleFunction(JT.tick, nil, timer.getTime() + 1)
end
