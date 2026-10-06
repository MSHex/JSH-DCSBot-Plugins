-- JSH CSAR plugin: mission environment
-- Loaded by the bot on servers where the plugin is enabled. Reports rescues to
-- the bot, which decides the credits from jsh_csar.yaml and records the counts.

if jsh_csar then return end
jsh_csar = {}
local CS = jsh_csar

CS.pollInterval = 5     -- seconds between attach attempts
CS.maxPolls     = 120   -- stop polling after ~10 min
CS.debug        = true

local function log(msg)
  if CS.debug then env.info("[jsh_csar] " .. msg) end
end

local function findPlayerUnit(name)
  for _, side in ipairs({ 1, 2 }) do
    for _, u in ipairs(coalition.getPlayers(side) or {}) do
      if u:isExist() and u:getPlayerName() == name then return u end
    end
  end
  return nil
end

local function playerOf(unit)
  if not unit or not unit.isExist or not unit:isExist() then return nil end
  return unit:getPlayerName()
end

-- counts = { ["Healthy"] = 1, ["Critical"] = 2, ["RedPilot"] = 1, ... }
function CS.report(name, counts, source)
  if not name or not counts or not next(counts) then return end
  if not dcsbot or not dcsbot.sendBotTable then
    log("dcsbot not available, dropped rescue report for " .. name)
    return
  end
  dcsbot.sendBotTable({ command = "jshCsarRescue", player = name,
                        counts = counts, source = source })
end

-- Called by the bot to pay a player.
function CS.pay(name, points, reason, announce)
  if not dcsbot or not dcsbot.addUserPoints then
    log("dcsbot not available, could not pay " .. tostring(name))
    return
  end
  local ok, err = pcall(dcsbot.addUserPoints, name, points, reason)
  if not ok then
    log("addUserPoints failed: " .. tostring(err))
    return
  end
  if announce ~= false then
    local unit = findPlayerUnit(name)
    local grp = unit and unit:getGroup()
    if grp then
      trigger.action.outTextForGroup(grp:getID(),
        string.format("+%d server credits %s", points, reason), 10)
    end
  end
  log(string.format("%s +%d (%s)", name, points, reason))
end

---------------------------------------------------------------- JOKER ADV_CSAR
-- ADV_CSAR.OnRescueCallback(clientObj, passengers) fires on drop-off at a
-- friendly airfield/FARP, and on auto-rescue (clientObj = nil, nobody to pay).
-- Each passenger carries .name, .coalition and .healthState.
-- V1.11+ exposes AddRescueCallback, so several listeners can coexist. On older
-- versions the existing single callback is kept and called first.
local function attachJoker()
  local handler = function(clientObj, passengers)
    pcall(function()
      if not clientObj then return end          -- auto-rescue at base, no rescuer
      local name = playerOf(clientObj)
      if not name then return end
      local side = clientObj:getCoalition()
      local counts = {}
      for _, pax in ipairs(passengers or {}) do
        local key
        if pax.coalition and pax.coalition ~= side then
          key = "RedPilot"                      -- captured enemy pilot
        else
          key = pax.healthState or "Healthy"
        end
        counts[key] = (counts[key] or 0) + 1
      end
      CS.report(name, counts, "joker")
    end)
  end

  if type(ADV_CSAR.AddRescueCallback) == "function" then
    ADV_CSAR.AddRescueCallback(handler)
  else
    local previous = ADV_CSAR.OnRescueCallback
    ADV_CSAR.OnRescueCallback = function(clientObj, passengers)
      if type(previous) == "function" then pcall(previous, clientObj, passengers) end
      handler(clientObj, passengers)
    end
  end
end

---------------------------------------------------------------- FOOTHOLD
local function footholdActive()
  return type(BattleCommander) == "table"
end

-- Foothold banks "Pilot Rescue" when the player lands at a friendly base.
local function attachFoothold()
  local original = BattleCommander.commitTempStats
  BattleCommander.commitTempStats = function(self, playerName, ...)
    local rescued = 0
    pcall(function()
      local stats = self.tempStats and self.tempStats[playerName]
      rescued = tonumber(stats and stats['Pilot Rescue']) or 0
    end)
    local results = { original(self, playerName, ...) }
    if rescued > 0 then
      pcall(CS.report, playerName, { Unknown = rescued }, "foothold")
    end
    return unpack(results)
  end
end

---------------------------------------------------------------- CIRIBOB CSAR
local function attachCiribobCSAR()
  local original = csar.rescuePilots
  csar.rescuePilots = function(heliUnit, ...)
    local count = 0
    local ok, heliName = pcall(function() return heliUnit:getName() end)
    if ok and csar.inTransitGroups and csar.inTransitGroups[heliName] then
      for _ in pairs(csar.inTransitGroups[heliName]) do count = count + 1 end
    end
    local results = { original(heliUnit, ...) }
    if count > 0 then
      pcall(function() CS.report(playerOf(heliUnit), { Unknown = count }, "ciribob") end)
    end
    return unpack(results)
  end
end

---------------------------------------------------------------- MOOSE Ops.CSAR
local function attachMooseCSAR(obj)
  local previous = obj.OnAfterRescued
  function obj:OnAfterRescued(From, Event, To, HeliUnit, HeliName, PilotsSaved)
    if previous then previous(self, From, Event, To, HeliUnit, HeliName, PilotsSaved) end
    pcall(function()
      local unit = HeliUnit and HeliUnit.GetDCSObject and HeliUnit:GetDCSObject()
      CS.report(playerOf(unit), { Unknown = PilotsSaved or 1 }, "moose")
    end)
  end
end

local function findMooseInstances(className)
  local found = {}
  local class = rawget(_G, className)
  for _, v in pairs(_G) do
    if type(v) == "table" and v ~= class then
      local ok, cn = pcall(function() return v.ClassName end)
      if ok and cn == className then found[#found + 1] = v end
    end
  end
  return found
end

---------------------------------------------------------------- POLLER
local attached, polls = {}, 0

local function try(key, isReady, attachFn)
  if attached[key] or not isReady() then return end
  attached[key] = true
  local ok, err = pcall(attachFn)
  log(ok and ("attached " .. key) or ("attach " .. key .. " failed: " .. tostring(err)))
end

local function poll()
  polls = polls + 1
  try("joker", function()
    return type(ADV_CSAR) == "table" and type(ADV_CSAR.rescueHelos) == "table"
  end, attachJoker)
  try("foothold", function()
    return footholdActive() and type(BattleCommander.commitTempStats) == "function"
  end, attachFoothold)
  try("ciribob", function()
    return type(csar) == "table" and type(csar.rescuePilots) == "function"
  end, attachCiribobCSAR)
  for _, obj in ipairs(findMooseInstances("CSAR")) do
    try("moose:" .. tostring(obj), function() return true end,
      function() attachMooseCSAR(obj) end)
  end
  if polls >= CS.maxPolls then
    log("polling stopped")
    return nil
  end
  return timer.getTime() + CS.pollInterval
end

timer.scheduleFunction(poll, nil, timer.getTime() + CS.pollInterval)
log("loaded")
