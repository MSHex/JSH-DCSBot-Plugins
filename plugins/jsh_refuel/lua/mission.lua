-- JSH Refuel: mission environment
--
-- Measures how much fuel a player takes from a tanker and reports it. The bot
-- decides the credits -- the rate, the minimum and the per-sortie cap all live
-- in jsh_refuel.yaml, so this file never has to be redeployed to change policy.

if jsh_refuel then return end
jsh_refuel = {}
local RF = jsh_refuel

-- Bind the bot's table explicitly, the way DCSServerBot's own plugin mission
-- scripts do. Without it every report is a silent no-op if DCSServerBot.lua
-- has not loaded.
local base = _G
dcsbot = dcsbot or base.dcsbot

RF.debug = true
RF.pending = {}            -- unit name -> fuel state when the boom went in

local KG_TO_LBS = 2.20462262185

local function log(msg)
  if RF.debug then env.info("[jsh_refuel] " .. msg) end
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
  if not unit or not unit.getPlayerName then return nil end
  local ok, name = pcall(function() return unit:getPlayerName() end)
  if ok and name and name ~= "" then return name end
  return nil
end

-- getFuel() is a fraction of INTERNAL capacity, and getDesc().fuelMassMax is
-- that capacity in kilograms, so the change between the two readings converts
-- straight to pounds. Fuel in external tanks sits outside fuelMassMax, so a
-- drop-tank fill reads low; the figure only has to be consistent, not exact.
local function fuelState(unit)
  local ok, state = pcall(function()
    local desc = unit:getDesc() or {}
    return { fuel = unit:getFuel(), massMax = desc.fuelMassMax or 0 }
  end)
  if ok and state and type(state.fuel) == "number" and state.massMax > 0 then
    return state
  end
  return nil
end

function RF.report(name, lbs, unit)
  if not dcsbot or not dcsbot.sendBotTable then
    if not RF.warnedNoBot then
      RF.warnedNoBot = true
      env.error("[jsh_refuel] cannot report: DCSServerBot.lua is not loaded in this "
                .. "mission, so no refuelling will ever be paid")
    end
    return
  end
  local aircraft
  pcall(function() aircraft = unit:getTypeName() end)
  dcsbot.sendBotTable({ command = "jshRefuelDone", player = name,
                        lbs = lbs, aircraft = aircraft })
  log(name .. " took " .. lbs .. " lbs")
end

-- Called by the bot once it has worked out what the fuel was worth.
function RF.pay(name, points, reason, announce)
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
      trigger.action.outTextForGroup(grp:getID(), reason, 15)
    end
  end
end

-- S_EVENT_REFUELING names the receiving aircraft as the initiator, so the
-- player taking on the fuel is the one credited. A tanker flown by a human
-- earns nothing for giving fuel away; that would be a separate reward.
local handler = {}

function handler:onEvent(event)
  -- Never let this raise. A throw inside onEvent aborts the whole event for
  -- every handler after it, and costs far more than a missed refuelling.
  local ok, err = pcall(function()
    if not event or not event.initiator then return end
    local id = event.id
    if id ~= world.event.S_EVENT_REFUELING and id ~= world.event.S_EVENT_REFUELING_STOP then
      return
    end

    local unit = event.initiator
    local name = playerOf(unit)
    if not name then return end          -- AI receiver, or not an aircraft
    local unitName = unit:getName()

    if id == world.event.S_EVENT_REFUELING then
      local state = fuelState(unit)
      if state then
        RF.pending[unitName] = { player = name, fuel = state.fuel, massMax = state.massMax }
      end
      return
    end

    -- S_EVENT_REFUELING_STOP: the boom came out, so settle up.
    local start = RF.pending[unitName]
    RF.pending[unitName] = nil
    if not start or start.player ~= name then return end
    local state = fuelState(unit)
    if not state then return end

    -- The aircraft burns while it drinks, so this understates the transfer
    -- slightly. It can also go negative on a disconnect with no fuel taken.
    local lbs = math.floor((state.fuel - start.fuel) * start.massMax * KG_TO_LBS)
    if lbs <= 0 then return end
    RF.report(name, lbs, unit)
  end)
  if not ok and not RF.warnedHandler then
    RF.warnedHandler = true
    env.error("[jsh_refuel] event handler error (suppressed from here on): " .. tostring(err))
  end
end

world.addEventHandler(handler)

log("loaded")
