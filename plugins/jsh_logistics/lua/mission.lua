-- JSH Logistics: mission environment
--
-- Two jobs, both reported to the bot, which decides the credits from
-- jsh_logistics.yaml per instance:
--
--  1. Foothold deliveries. Foothold counts every logistics job -- zone
--     capture, upgrade and resupply by cargo or troops, warehouse deliveries,
--     CTLD builds -- as a per-player TEMP stat, and banks the lot when the
--     pilot lands at a friendly base (BattleCommander:commitTempStats). We
--     wrap that one method on the live `bc`, so what we report is exactly
--     what Foothold itself just paid out for: lost if the pilot dies first,
--     banked once on landing. Foothold's source is untouched.
--
--  2. Airtime. Every few seconds, each player in a configured logistics
--     aircraft that is airborne adds to their bucket. On landing (airborne,
--     then on the ground, aircraft intact) the bucket is reported; if the
--     aircraft is lost or the pilot leaves it first, the bucket is dropped.
--
-- Loading twice is safe and on purpose: functions are redefined, so a bot
-- restart brings a running mission up to date. State survives in the table.

jsh_logistics = jsh_logistics or {}
local JL = jsh_logistics

local base = _G
dcsbot = dcsbot or base.dcsbot

JL.debug = true
JL.cfg = JL.cfg or { types = {}, interval = 10 }
JL.air = JL.air or {}            -- [playerName] = { unit=, seconds=, airborne= }

local function log(msg)
  if JL.debug then env.info("[jsh_logistics] " .. msg) end
end

local function send(payload)
  if dcsbot and dcsbot.sendBotTable then
    dcsbot.sendBotTable(payload)
  elseif not JL.warnedNoBot then
    JL.warnedNoBot = true
    env.error("[jsh_logistics] DCSServerBot.lua is not loaded in this mission; nothing is reported")
  end
end

---------------------------------------------------------------- PAYMENT

-- The bot decides the amount; this only credits it, the way jsh_csar does.
function JL.pay(name, points, reason, announce)
  if not dcsbot or not dcsbot.addUserPoints then
    return log("dcsbot not available, could not pay " .. tostring(name))
  end
  local ok, err = pcall(dcsbot.addUserPoints, name, points, reason)
  if not ok then return log("addUserPoints failed: " .. tostring(err)) end
  if announce ~= false then
    for _, side in ipairs({ 1, 2 }) do
      for _, u in ipairs(coalition.getPlayers(side) or {}) do
        if u:isExist() and u:getPlayerName() == name then
          local g = u:getGroup()
          if g then
            trigger.action.outTextForGroup(g:getID(),
              string.format("+%d server credits %s", points, reason), 10)
          end
        end
      end
    end
  end
  log(string.format("%s +%d (%s)", name, points, reason))
end

---------------------------------------------------------------- FOOTHOLD

-- Copies the numeric stats, which is all the bot needs and all that is safe
-- to send as JSON.
local function snapshot(stats)
  local out, any = {}, false
  for k, v in pairs(stats or {}) do
    if type(k) == "string" and type(v) == "number" and v > 0 and k ~= "Points" then
      out[k] = v
      any = true
    end
  end
  return any and out or nil
end

function JL.attach()
  local bc = base.bc
  if type(bc) ~= "table" or type(bc.commitTempStats) ~= "function" then
    return false
  end
  if bc.__jshLogisticsWrapped then return true end
  local original = bc.commitTempStats
  bc.__jshLogisticsOriginal = original
  bc.commitTempStats = function(self, playerName, ...)
    -- Read BEFORE the original: it resets the temp stats it commits.
    local stats = snapshot(self.tempStats and self.tempStats[playerName])
    local result = original(self, playerName, ...)
    if stats and playerName then
      local ok, err = pcall(send, { command = "jshLogisticsBanked",
                                    player = playerName, stats = stats })
      if not ok then log("report failed: " .. tostring(err)) end
    end
    return result
  end
  bc.__jshLogisticsWrapped = true
  log("attached to BattleCommander")
  return true
end

-- Foothold may still be starting when we load: try for ten minutes. A
-- mission without Foothold simply never attaches, and airtime still works.
JL.attachTries = 0
function JL.attachLoop()
  if JL.attach() then return nil end
  JL.attachTries = JL.attachTries + 1
  if JL.attachTries >= 120 then
    log("no Foothold BattleCommander found; delivery rewards are off in this mission")
    return nil
  end
  return timer.getTime() + 5
end

---------------------------------------------------------------- AIRTIME

function JL.configure(cfg)
  JL.cfg = cfg or JL.cfg
  local set = {}
  for _, t in ipairs(JL.cfg.types or {}) do set[string.lower(t)] = true end
  JL.typeSet = set
  log("airtime: " .. tostring(#(JL.cfg.types or {})) .. " aircraft type(s), every "
      .. tostring(JL.cfg.interval) .. " s")
end

local function flush(name, entry, typeName)
  if entry.seconds > 0 then
    send({ command = "jshLogisticsAirtime", player = name,
           seconds = entry.seconds, type = typeName })
  end
  entry.seconds = 0
end

function JL.sample()
  local step = tonumber(JL.cfg.interval) or 10
  local set = JL.typeSet or {}
  local seen = {}
  if next(set) then
    for _, side in ipairs({ 1, 2 }) do
      for _, u in ipairs(coalition.getPlayers(side) or {}) do
        local ok, name = pcall(u.getPlayerName, u)
        if ok and name and u:isExist() then
          local typeName = u:getTypeName()
          if set[string.lower(typeName or "")] then
            seen[name] = true
            local unitName = u:getName()
            local entry = JL.air[name]
            if not entry or entry.unit ~= unitName then
              -- A new aircraft: whatever was in the old bucket was not landed.
              entry = { unit = unitName, seconds = 0, airborne = false }
              JL.air[name] = entry
            end
            if u:inAir() then
              entry.seconds = entry.seconds + step
              entry.airborne = true
            elseif entry.airborne then
              -- Airborne last time, on the ground now, aircraft intact: landed.
              entry.airborne = false
              flush(name, entry, typeName)
            end
          end
        end
      end
    end
  end
  -- Not in a logistics aircraft any more: crashed, ejected, or changed slot
  -- in the air. Unlanded time is not paid.
  for name in pairs(JL.air) do
    if not seen[name] then JL.air[name] = nil end
  end
  return timer.getTime() + step
end

---------------------------------------------------------------- START

if not JL.started then
  JL.started = true
  timer.scheduleFunction(function() return JL.attachLoop() end, nil, timer.getTime() + 1)
  timer.scheduleFunction(function() return JL.sample() end, nil, timer.getTime() + 5)
else
  JL.attach()     -- a reload: re-check Foothold, keep the running timers
end
log("mission environment loaded")
