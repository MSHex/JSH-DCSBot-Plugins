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

function JR.attach()
  if JR.attached then return end
  if type(BattleCommander) ~= "table" or type(BattleCommander.getPlayerRank) ~= "function" then
    return
  end

  local originalRank = BattleCommander.getPlayerRank
  BattleCommander.getPlayerRank = function(self, playerName, ...)
    local level = playerName and JR.levels[playerName]
    if level then return level end
    -- unknown pilot: let Foothold answer, so nothing is worse off than before
    return originalRank(self, playerName, ...)
  end
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
  if JR.namesAttached then return end
  if not next(JR.names or {}) then return end
  if type(BattleCommander) ~= "table" or type(BattleCommander.getRankName) ~= "function" then
    return
  end

  local originalName = BattleCommander.getRankName
  BattleCommander.getRankName = function(self, idx, ...)
    local name = idx and JR.names[tostring(idx)]
    if name then return name end
    return originalName(self, idx, ...)
  end
  JR.originalGetRankName = originalName
  JR.namesAttached = true
  log("rank names attached")
end

-- The bot normally calls set() straight after loading this file, but attach
-- anyway in case Foothold finishes initialising first.
pcall(JR.attach)
pcall(JR.attachNames)

log("loaded")
