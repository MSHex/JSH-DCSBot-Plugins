-- Airport plugin: mission environment
-- Loaded by the bot on servers where the plugin is enabled. Applies airbase
-- warehouse changes when the bot asks it to.

if jsh_airport then return end
jsh_airport = {}
local AP = jsh_airport

AP.debug = true

local function log(msg)
  if AP.debug then env.info("[jsh_airport] " .. msg) end
end

local function reply(requestId, ok, err, failed)
  if dcsbot and dcsbot.sendBotTable then
    dcsbot.sendBotTable({ command = "jshAirportResult", request_id = requestId,
                          ok = ok, error = err, failed = failed })
  end
end

---------------------------------------------------------------- WAREHOUSES
local LIQUIDS = { jet_fuel = 0, avgas = 1, mw50 = 2, diesel = 3 }

local function getWarehouse(airbaseName)
  local ab = Airbase.getByName(airbaseName)
  if not ab then return nil, "airbase not found: " .. tostring(airbaseName) end
  local wh = ab:getWarehouse()
  if not wh then return nil, "no warehouse at " .. tostring(airbaseName) end
  return wh
end

-- preset = { liquids = { ["0"] = kg, ... }, items = { [name] = count } }
-- Liquids: 0 jet fuel, 1 avgas, 2 MW50, 3 diesel (as in the /airbase info sheet).
-- Values are absolute. Anything not listed is left unchanged.
function AP.applyWarehouse(requestId, airbaseName, preset)
  local wh, err = getWarehouse(airbaseName)
  if not wh then reply(requestId, false, err) return end
  local failed = {}
  for liquid, amount in pairs(preset.liquids or {}) do
    local t = LIQUIDS[liquid] or tonumber(liquid)
    local ok = t and pcall(wh.setLiquidAmount, wh, t, amount)
    if not ok then failed[#failed + 1] = tostring(liquid) end
  end
  for item, count in pairs(preset.items or {}) do
    local ok = pcall(wh.setItem, wh, item, count)
    if not ok then failed[#failed + 1] = tostring(item) end
  end
  if #failed > 0 then
    reply(requestId, false, #failed .. " items not accepted", failed)
  else
    reply(requestId, true)
  end
  log("warehouse " .. airbaseName .. " updated, failures: " .. #failed)
end

-- Names of every airbase, FARP and ship in the running mission.
function AP.listAirbases(requestId)
  local list = {}
  local ok = pcall(function()
    for _, ab in pairs(world.getAirbases() or {}) do
      local desc = ab:getDesc()
      local category = "AIRDROME"
      if desc and desc.category == Airbase.Category.HELIPAD then
        category = "FARP"
      elseif desc and desc.category == Airbase.Category.SHIP then
        category = "SHIP"
      end
      list[#list + 1] = { name = ab:getName(), category = category,
                          coalition = ab:getCoalition() }
    end
  end)
  if dcsbot and dcsbot.sendBotTable then
    dcsbot.sendBotTable({ command = "jshAirportResult", request_id = requestId,
                          ok = ok, airbases = list })
  end
  log("listed " .. #list .. " airbases")
end

log("loaded")
