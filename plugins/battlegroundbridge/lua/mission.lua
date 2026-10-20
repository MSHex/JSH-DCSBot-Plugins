-- BattlegroundBridge: mission environment (Lua 5.1 / DCS)
--
-- Captures F10 marker commands and holds them until the bot collects them.
-- Nothing here knows anything about the database or Battleground; it reports
-- what a player typed, where, and who typed it, and forgets an event only when
-- the bot confirms that exact event.
--
-- Guarded so a second load is a no-op. The bot sends the load command on every
-- mission start AND on every registration, so this runs more than once in a
-- normal day -- registering the world event handler twice would double every
-- marker event.
if jsh_bgb then return end
jsh_bgb = {}
local B = jsh_bgb

local base = _G
dcsbot = dcsbot or base.dcsbot

B.debug = true

-- Limits. The transport truncates somewhere around 2,300 characters, and a
-- silently shortened command is worse than a rejected one, so anything longer
-- is reported with its length instead of being cut down.
B.INPUT_MAX = 2200
B.MAX_MARKERS = 500
B.MAX_SNAPSHOT = 50
-- Players type a title and then keep editing it. Two seconds of quiet means the
-- bot sees one finished command rather than every keystroke-sized change event.
B.QUIET_SECONDS = 2

B.token = nil          -- set once, by the FIRST snapshot request of this mission
B.pending = {}         -- markerId -> event
B.pendingCount = 0
B.sequence = 0
B.overflowed = false

local function log(msg)
  if B.debug then env.info('[bgb] ' .. msg) end
end

local function send(payload)
  if dcsbot and dcsbot.sendBotTable then
    dcsbot.sendBotTable(payload)
  elseif not B.warnedNoBot then
    B.warnedNoBot = true
    env.error('[bgb] cannot reply: DCSServerBot.lua is not loaded in this mission, '
              .. 'so every marker command will be ignored')
  end
end

---------------------------------------------------------------- identity
--
-- The marker's visible text proves nothing about who wrote it, so identity
-- comes from the event initiator. A unit name and a player name together are
-- the only accepted combination: GM and spectator marker events frequently
-- arrive with no usable initiator at all, and guessing which connected player
-- it "probably" was is how one pilot ends up able to delete another's reports.
local function identify(event)
  local ok, result = pcall(function()
    local unit = event.initiator
    if not unit or not unit.getPlayerName then return nil end
    local playerName = unit:getPlayerName()
    local unitName = unit.getName and unit:getName() or nil
    if not playerName or not unitName then return nil end
    return { player_name = playerName, unit_name = unitName }
  end)
  if ok then return result end
  return nil
end

---------------------------------------------------------------- position

local function positionOf(event)
  local ok, result = pcall(function()
    local pos = event.pos
    if not pos then return nil end
    local lat, lon = coord.LOtoLL(pos)
    local grid = coord.LLtoMGRS(lat, lon)
    local mgrs = nil
    if grid and grid.UTMZone and grid.MGRSDigraph then
      -- 15 characters, which is the form the website and the stock
      -- battleground2 plugin both expect (e.g. 38TLN0274366889).
      mgrs = string.format('%s%s%05d%05d', grid.UTMZone, grid.MGRSDigraph,
                           grid.Easting or 0, grid.Northing or 0)
    end
    return { lat = lat, lon = lon, mgrs = mgrs }
  end)
  if ok then return result end
  return nil
end

---------------------------------------------------------------- capture

local function remember(event, text)
  local idx = event.idx
  if idx == nil then return end

  local existing = B.pending[idx]
  if not existing and B.pendingCount >= B.MAX_MARKERS then
    -- Visible rather than silent: a full queue means marker commands are being
    -- dropped, and the only way anyone finds out is this log line.
    if not B.overflowed then
      B.overflowed = true
      env.error(string.format('[bgb] pending marker queue is full (%d); further new '
                              .. 'marker commands are being DROPPED until the bot drains it',
                              B.MAX_MARKERS))
    end
    return
  end

  B.sequence = B.sequence + 1
  local who = identify(event)
  local where = positionOf(event)

  -- Latest event per marker wins. An edit supersedes the edit before it, which
  -- is why this overwrites rather than appends.
  B.pending[idx] = {
    marker = idx,
    sequence = B.sequence,
    at = timer.getTime(),
    text = string.sub(text or '', 1, B.INPUT_MAX + 1),
    length = string.len(text or ''),
    coalition = event.coalition,
    player_name = who and who.player_name or nil,
    unit_name = who and who.unit_name or nil,
    lat = where and where.lat or nil,
    lon = where and where.lon or nil,
    mgrs = where and where.mgrs or nil,
  }
  if not existing then B.pendingCount = B.pendingCount + 1 end
end

B.handler = {}

function B.handler:onEvent(event)
  if not event or not event.id then return end
  -- Added and changed only.
  --
  -- S_EVENT_MARK_REMOVED is deliberately NOT handled: removing an F10 marker
  -- must not delete a submitted report. Intel outlives the marker that created
  -- it, and deletion is an explicit command.
  if event.id == world.event.S_EVENT_MARK_ADDED
     or event.id == world.event.S_EVENT_MARK_CHANGE then
    local ok, err = pcall(remember, event, event.text)
    if not ok then env.error('[bgb] marker capture failed: ' .. tostring(err)) end
  end
end

world.addEventHandler(B.handler)

---------------------------------------------------------------- bot interface

--- Hand the bot everything that has settled, and report our mission token.
--
-- The candidate token matters. The bot cannot know whether this mission is the
-- one it was talking to before, so it offers a freshly generated token on every
-- request and we adopt it ONLY if we have none yet. A reloaded mission runs this
-- file again with B.token nil and therefore takes the new one; a bot restart
-- against a still-running mission finds B.token already set and keeps it, so
-- the existing marker-to-report mappings stay valid.
--
-- Initialising from a token the bot remembered from the PREVIOUS mission is the
-- specific bug this shape avoids: it would make a new mission silently inherit
-- the old one's marker mappings.
function B.snapshot(requestId, candidateToken)
  if B.token == nil and candidateToken and candidateToken ~= '' then
    B.token = candidateToken
    log('mission token initialised as ' .. tostring(B.token))
  end

  local now = timer.getTime()
  local events = {}
  for _, pending in pairs(B.pending) do
    if #events >= B.MAX_SNAPSHOT then break end
    if (now - pending.at) >= B.QUIET_SECONDS then
      events[#events + 1] = pending
    end
  end

  send({
    command = 'jshBgbSnapshot',
    request_id = requestId,
    token = B.token,
    mission_name = env.mission and env.mission.theatre or nil,
    events = events,
    pending_total = B.pendingCount,
    overflowed = B.overflowed,
    input_max = B.INPUT_MAX,
  })
end

--- Forget the exact events the bot has committed.
--
-- Matched on sequence, not just marker id. If a player edited a marker again
-- while the bot was busy, the pending entry now holds a NEWER sequence and must
-- survive the acknowledgement of the older one -- otherwise that edit is lost
-- with no trace.
function B.ack(token, acknowledged)
  if token ~= B.token then
    log('ignoring an acknowledgement for token ' .. tostring(token)
        .. ' (ours is ' .. tostring(B.token) .. ')')
    return
  end
  for _, item in ipairs(acknowledged or {}) do
    local pending = B.pending[item.marker]
    if pending and pending.sequence == item.sequence then
      B.pending[item.marker] = nil
      B.pendingCount = B.pendingCount - 1
    end
  end
  if B.pendingCount < B.MAX_MARKERS then B.overflowed = false end
end

log('mission environment loaded')
