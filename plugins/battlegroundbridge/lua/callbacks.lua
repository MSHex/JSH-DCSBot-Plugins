-- BattlegroundBridge: hook callbacks.
--
-- Registers its own user callbacks table. It does not touch missionstats or any
-- other plugin's handler.
local utils = require('DCSServerBotUtils')
local bgb = {}

function bgb.onMissionLoadEnd()
    -- A new mission means a new mission Lua environment, so jsh_bgb is gone and
    -- the next snapshot request will adopt a fresh token. That is exactly the
    -- signal the bot uses to tell a reloaded mission from a bot restart.
    utils.loadScript('DCSServerBot.lua')
    utils.loadScript('battlegroundbridge/mission.lua')
    log.write('DCSServerBot', log.DEBUG, 'BgBridge: mission.lua loaded on mission start')
end

Sim.setUserCallbacks(bgb)
