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

-- Replaces the whole table each time: a player whose rank the bot no longer
-- sends falls back to Foothold's own answer rather than keeping a stale level.
function JR.set(levels, names)
  JR.levels = levels or {}
  JR.names = names or {}
  local n = 0
  for _ in pairs(JR.levels) do n = n + 1 end
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
