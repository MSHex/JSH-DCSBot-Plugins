-- Hook environment: commands sent from the bot (Python) to DCS.
local base   = _G
local dcsbot = base.dcsbot

-- Loads mission.lua into the running mission. Sent by the bot only for
-- servers where the plugin is enabled in jsh_logistics.yaml. The file is
-- read with dofile, not sent over UDP, so it has no 8192-byte limit.
function dcsbot.jshLogisticsLoad(json)
    local path = lfs.writedir():gsub('\\', '/') .. 'Scripts/net/DCSServerBot/jsh_logistics/mission.lua'
    local file = io.open(path, 'r')
    if not file then
        log.write('DCSServerBot', log.ERROR, 'JSH Logistics: mission.lua NOT FOUND at ' .. path ..
            ' - jsh_logistics is not in the opt_plugins of this node, or the bot has not' ..
            ' been restarted since it was added.')
        return
    end
    file:close()
    log.write('DCSServerBot', log.DEBUG, 'JSH Logistics: loading ' .. path)
    net.dostring_in('mission', 'a_do_script("dofile(\\"' .. path .. '\\")")')
end

-- Runs a Lua snippet inside the mission scripting environment.
function dcsbot.jshLogisticsRun(json)
    net.dostring_in('mission', 'a_do_script(' .. string.format('%q', json.lua) .. ')')
end
