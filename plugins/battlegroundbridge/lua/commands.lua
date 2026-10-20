-- BattlegroundBridge: hook environment. Commands sent from the bot to DCS.
local utils  = require('DCSServerBotUtils')
local base   = _G
local dcsbot = base.dcsbot

-- Loads mission.lua into the running mission. mission.lua guards against a
-- second load, so sending this more than once is safe and is in fact normal:
-- the bot sends it on mission start and on every registration.
function dcsbot.jshBgbLoad(json)
    log.write('DCSServerBot', log.DEBUG, 'BgBridge: loading mission.lua')
    utils.loadScript('DCSServerBot.lua')
    utils.loadScript('battlegroundbridge/mission.lua')
end

-- Asks the mission for its settled marker events.
-- `token` is a freshly generated candidate the mission adopts only if it has
-- none yet -- see the comment on B.snapshot in mission.lua.
function dcsbot.jshBgbSnapshot(json)
    local script = string.format(
        'if jsh_bgb then jsh_bgb.snapshot(%s, %s) else dcsbot.sendBotTable({'
        .. 'command = "jshBgbSnapshot", request_id = %s, error = "mission.lua is not loaded" })'
        .. ' end',
        utils.basicSerialize(json.request_id),
        utils.basicSerialize(json.token),
        utils.basicSerialize(json.request_id))
    net.dostring_in('mission', 'a_do_script(' .. utils.basicSerialize(script) .. ')')
end

-- Confirms the events the bot has committed, so the mission can forget them.
--
-- The acknowledgement is nothing but integers, so it crosses into the mission
-- environment as a small Lua literal built here. It is NOT passed as JSON: the
-- `net` table exists only in this hook environment, so a json2lua call on the
-- far side would fail inside the mission where nobody would see it, and every
-- acknowledgement would be quietly lost while appearing to work.
--
-- tonumber() on both values is what keeps this a literal of numbers. Anything
-- that is not a number is dropped rather than written into the script.
function dcsbot.jshBgbAck(json)
    local parts = {}
    for _, item in pairs(json.acknowledged or {}) do
        local marker = tonumber(item.marker)
        local sequence = tonumber(item.sequence)
        if marker and sequence then
            parts[#parts + 1] = string.format('{marker=%d,sequence=%d}', marker, sequence)
        end
    end
    if #parts == 0 then return end
    local script = string.format('if jsh_bgb then jsh_bgb.ack(%s, {%s}) end',
                                 utils.basicSerialize(json.token),
                                 table.concat(parts, ','))
    net.dostring_in('mission', 'a_do_script(' .. utils.basicSerialize(script) .. ')')
end
