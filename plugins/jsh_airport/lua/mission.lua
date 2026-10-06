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
-- Each airbase is read in its own pcall: one that raises (a destroyed base, a
-- ship with an odd desc) is skipped instead of discarding the whole list.
function AP.listAirbases(requestId)
  local list, skipped = {}, 0
  local bases = {}
  local gotBases = pcall(function() bases = world.getAirbases() or {} end)
  if gotBases then
    for _, ab in pairs(bases) do
      local ok, entry = pcall(function()
        local desc = ab:getDesc()
        local category = "AIRDROME"
        if desc and desc.category == Airbase.Category.HELIPAD then
          category = "FARP"
        elseif desc and desc.category == Airbase.Category.SHIP then
          category = "SHIP"
        end
        return { name = ab:getName(), category = category,
                 coalition = ab:getCoalition() }
      end)
      if ok and entry and entry.name then
        list[#list + 1] = entry
      else
        skipped = skipped + 1
      end
    end
  end
  if dcsbot and dcsbot.sendBotTable then
    dcsbot.sendBotTable({ command = "jshAirportResult", request_id = requestId,
                          ok = gotBases, airbases = list, skipped = skipped,
                          error = (not gotBases) and "world.getAirbases() failed" or nil })
  end
  log("listed " .. #list .. " airbases, skipped " .. skipped)
end

---------------------------------------------------------------- BATTLE DAMAGE
-- Scales everything in the warehouse by `keep` (0.75 for 25% damage, 0 for
-- 100%). Reads what is actually in the warehouse right now, so it accounts for
-- whatever players have already used, rather than working from a level sheet.
--
-- Inventory shape differs between item kinds and DCS versions, so the walk
-- handles both a flat {name = count} table and one nested a level deeper.
local function scaleTable(wh, tbl, keep, stats)
  for name, value in pairs(tbl or {}) do
    if type(value) == "number" then
      local before = value
      local after = math.floor(before * keep)
      if after ~= before then
        if pcall(wh.setItem, wh, name, after) then
          stats.changed = stats.changed + 1
          stats.before = stats.before + before
          stats.after = stats.after + after
        else
          stats.failed[#stats.failed + 1] = tostring(name)
        end
      else
        stats.before = stats.before + before
        stats.after = stats.after + after
      end
    elseif type(value) == "table" then
      scaleTable(wh, value, keep, stats)
    end
  end
end

function AP.damageWarehouse(requestId, airbaseName, keep)
  local wh, err = getWarehouse(airbaseName)
  if not wh then reply(requestId, false, err) return end

  local stats = { changed = 0, before = 0, after = 0, failed = {} }
  local inv = nil
  local gotInv = pcall(function() inv = wh:getInventory() end)
  if not gotInv or not inv then
    reply(requestId, false, "could not read the warehouse inventory at " .. tostring(airbaseName))
    return
  end

  scaleTable(wh, inv.aircraft, keep, stats)
  scaleTable(wh, inv.weapon, keep, stats)

  -- Liquids are keyed by type (0 jet fuel, 1 avgas, 2 MW50, 3 diesel) and need
  -- their own setter.
  local fuelBefore, fuelAfter = 0, 0
  for t = 0, 3 do
    local amount
    if pcall(function() amount = wh:getLiquidAmount(t) end) and type(amount) == "number" then
      local after = math.floor(amount * keep)
      fuelBefore = fuelBefore + amount
      fuelAfter = fuelAfter + after
      if after ~= amount then pcall(wh.setLiquidAmount, wh, t, after) end
    end
  end

  if dcsbot and dcsbot.sendBotTable then
    dcsbot.sendBotTable({ command = "jshAirportResult", request_id = requestId,
                          ok = true,
                          items_before = stats.before, items_after = stats.after,
                          fuel_before = fuelBefore, fuel_after = fuelAfter,
                          changed = stats.changed,
                          failed = (#stats.failed > 0) and stats.failed or nil })
  end
  log(string.format("%s damaged: items %d -> %d, liquids %d -> %d",
                    airbaseName, stats.before, stats.after, fuelBefore, fuelAfter))
end

log("loaded")
