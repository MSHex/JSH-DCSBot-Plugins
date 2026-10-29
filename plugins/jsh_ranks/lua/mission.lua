-- JSH Ranks: makes Foothold use the Discord rank ladder.
--
-- Foothold funnels every rank gate through BattleCommander:getPlayerRank --
-- shop items, tanker control, carrier navigation, the F10 menus -- so wrapping
-- that one function is enough to change what the whole mission enforces.
-- Foothold's own rank credits keep accruing underneath; they are simply no
-- longer what the gates consult.
--
-- The bot pushes the level table in with jsh_ranks.set().

jsh_ranks = jsh_ranks or {}
local JR = jsh_ranks

JR.levels = JR.levels or {}      -- player name -> Foothold rank level
JR.names  = JR.names  or {}      -- level -> display name (optional)
JR.debug  = true

JR.seen   = JR.seen   or {}      -- players we have answered for, logged once each
JR.missed = JR.missed or {}      -- players we had no rank for, logged once each

local function log(msg)
  if JR.debug then env.info("[jsh_ranks] " .. msg) end
end

-- Merges each push instead of replacing the table.
--
-- A push only ever carries the pilots the bot can see at that moment. Replacing
-- wholesale meant any push that omitted someone still flying -- a gap in the
-- player list, a slot transition, a momentarily empty read -- dropped their rank
-- until the next push, which can be refresh_minutes away. From inside the
-- mission that is indistinguishable from the plugin having failed: the pilot is
-- simply told their rank is insufficient.
--
-- Keeping an entry for a pilot who has left costs nothing, because it is
-- overwritten the moment they return. An empty push is ignored outright: it only
-- means nobody is on the server, and there is nothing there to govern.
function JR.set(levels, names)
  levels = levels or {}
  local n = 0
  for _ in pairs(levels) do n = n + 1 end
  if n > 0 then
    for name, level in pairs(levels) do
      JR.levels[name] = level
    end
  end
  -- Names come from the config and are always sent complete, so they replace.
  JR.names = names or {}
  log("received " .. n .. " rank(s)")
  JR.attach()
  JR.attachNames()
end

-- Re-checked on every set(). Foothold assigns these methods when
-- zoneCommanderv2.lua loads, so ours normally goes on top and stays -- but a
-- boolean latch would leave us silently detached for the rest of the mission if
-- anything ever replaced the function, which is exactly how attachNames broke
-- before. Marking our own wrapper tells "already ours" apart from "someone
-- replaced it", so we can re-attach instead of assuming we are still in place.
function JR.attach()
  if type(BattleCommander) ~= "table" or type(BattleCommander.getPlayerRank) ~= "function" then
    return
  end
  if BattleCommander.getPlayerRank == JR.wrappedGetPlayerRank then return end
  if JR.wrappedGetPlayerRank then log("getPlayerRank was replaced - re-attaching") end

  local originalRank = BattleCommander.getPlayerRank
  local wrapper = function(self, playerName, ...)
    -- Always call through. Since Foothold 5.3.0 getPlayerRank returns a second
    -- value, the pilot's career record, which newer callers pass straight back
    -- in; dropping it makes them re-read it. getPlayerRank never creates a
    -- record (_careerPlayerRecord is called with create=false), so this is a
    -- read and changes nothing on Foothold's side.
    local theirLevel, record = originalRank(self, playerName, ...)
    local level = playerName and JR.levels[playerName]
    if level then
      -- Logged once per pilot: enough to prove the override is being consulted
      -- and with what, without filling dcs.log on every menu refresh.
      if not JR.seen[playerName] then
        JR.seen[playerName] = true
        log(playerName .. ": level " .. tostring(level) ..
            " (Foothold would have said " .. tostring(theirLevel) .. ")")
      end
      return level, record
    end
    -- Unknown pilot: let Foothold answer, so nothing is worse off than before.
    -- Still worth one line, because a pilot the bot never pushed a rank for is
    -- the difference between "the hook is broken" and "the ladder didn't map".
    if playerName and not JR.missed[playerName] then
      JR.missed[playerName] = true
      log("no rank pushed for " .. playerName .. " - falling back to Foothold's "
          .. tostring(theirLevel))
    end
    return theirLevel, record
  end

  BattleCommander.getPlayerRank = wrapper
  JR.wrappedGetPlayerRank = wrapper
  JR.originalGetPlayerRank = originalRank
  JR.attached = true
  log("attached to BattleCommander")
end

-- Optional: show our rank names where Foothold would print its own. Several
-- Discord ranks map onto one Foothold level, so this names the band, not the
-- pilot's exact rank -- left off unless level_names is configured.
--
-- Separate from attach() and retried on every set(), because the first push
-- after a mission start usually carries no names at all (nobody is connected
-- yet). Folding it into attach() meant that first empty push latched
-- JR.attached and the names never went on for the life of the mission.
function JR.attachNames()
  if not next(JR.names or {}) then return end
  if type(BattleCommander) ~= "table" or type(BattleCommander.getRankName) ~= "function" then
    return
  end
  -- Same marker check as attach(), for the same reason.
  if BattleCommander.getRankName == JR.wrappedGetRankName then return end

  local originalName = BattleCommander.getRankName
  local wrapper = function(self, idx, ...)
    local name = idx and JR.names[tostring(idx)]
    if name then return name end
    return originalName(self, idx, ...)
  end
  BattleCommander.getRankName = wrapper
  JR.wrappedGetRankName = wrapper
  JR.originalGetRankName = originalName
  JR.namesAttached = true
  log("rank names attached")
end

-- The bot normally calls set() straight after loading this file, but attach
-- anyway in case Foothold finishes initialising first.
pcall(JR.attach)
pcall(JR.attachNames)

log("loaded")

-- ---------------------------------------------------------------- cbuy:
--
-- Foothold's own `buy:` marker cannot work from a Tactical Commander or Game
-- Master slot. DCS attaches no initiator to a mark placed from a slot with no
-- controllable unit, so zoneCommanderv2.lua leaves myRank at 0 and refuses
-- every gated item. Mission-side Lua cannot identify such a player either --
-- coalition.getPlayers() returns units, and a commander has none.
--
-- The bot can: it knows who is connected and what slot each of them occupies.
-- So it pushes the commanding level per coalition and we authorise against
-- that, then call Foothold's own buyShopItem, which has no rank check of its
-- own. Nothing in Foothold is modified, so a Foothold update cannot undo this.
--
-- `cbuy:` rather than `buy:` on purpose: Foothold's handler would still fire on
-- `buy:` and print "insufficient rank" beside our successful purchase, and we
-- cannot stop its handler running without editing it. Foothold matches nothing
-- on `cbuy:`, so only this code answers.

JR.commanders = JR.commanders or {}   -- coalition (as a string) -> level
JR.handledMarks = JR.handledMarks or {}

function JR.setCommanders(levels)
  JR.commanders = levels or {}
end

-- The level to authorise against: the pilot's own when DCS named them, else
-- whoever the bot says is commanding that coalition.
function JR.buyLevel(event)
  local name
  local unit = event.initiator
  if unit and unit.getPlayerName then
    local ok, got = pcall(function() return unit:getPlayerName() end)
    if ok then name = got end
  end
  if name and JR.levels[name] then return JR.levels[name], name end
  local side = event.coalition
  return tonumber(JR.commanders[tostring(side)] or JR.commanders[side] or 0) or 0, nil
end

function JR.doBuy(event, item)
  local side = event.coalition
  if type(bc) ~= 'table' or type(bc.buyShopItem) ~= 'function' then
    trigger.action.outTextForCoalition(side, 'cbuy: the shop is not available yet.', 10)
    return
  end
  local entry = bc.shops and bc.shops[side] and bc.shops[side][item]
  if not entry then
    trigger.action.outTextForCoalition(side, 'cbuy: no shop item called "' .. tostring(item) .. '".', 15)
    return
  end
  local level, name = JR.buyLevel(event)
  local need = tonumber(entry.reqRank) or 0
  -- Mirrors Foothold's own gate, which buyShopItem does not apply itself.
  if RankingSystem and need > 0 and level < need then
    trigger.action.outTextForCoalition(side, string.format(
      'cbuy %s: rank %d required, you are rank %d.', tostring(item), need, level), 15)
    log('cbuy refused: ' .. tostring(item) .. ' needs ' .. need .. ', had ' .. level)
    return
  end
  log('cbuy ' .. tostring(item) .. ' by ' .. (name or ('commander of coalition ' .. tostring(side)))
      .. ' at level ' .. level)
  bc:buyShopItem(side, item, nil, nil, nil)
end

function JR.onMarkEvent(event)
  -- 25 = MARK_ADDED, 26 = MARK_CHANGE. Typing text into a new mark fires both,
  -- so the idx is remembered to keep one command from buying twice.
  if not event or (event.id ~= 25 and event.id ~= 26) then return end
  if type(event.text) ~= 'string' then return end
  local item = event.text:match('^%s*[Cc][Bb][Uu][Yy]%s*:%s*(.-)%s*$')
  if not item or item == '' then return end
  if event.coalition ~= 1 and event.coalition ~= 2 then return end
  local idx = event.idx
  if idx then
    if JR.handledMarks[idx] then return end
    JR.handledMarks[idx] = true
  end
  JR.doBuy(event, item)
  if idx then pcall(trigger.action.removeMark, idx) end
end

if not JR.markHandler then
  JR.markHandler = { onEvent = function(_, e) pcall(JR.onMarkEvent, e) end }
  pcall(world.addEventHandler, JR.markHandler)
  log('cbuy handler registered')
end
