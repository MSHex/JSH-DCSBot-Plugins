-- Hook environment: commands sent from the bot (Python) to DCS.
local base   = _G
local dcsbot = base.dcsbot

-- Loads mission.lua into the running mission.
--
-- The bot copies this plugin's lua folder into
-- Saved Games/<instance>/Scripts/net/DCSServerBot/jsh_refuel/ at BOT startup,
-- for every plugin in that node's opt_plugins. If the plugin was never added to
-- this node, or the bot has not been restarted since it was, the file is simply
-- absent and dofile fails inside the mission where nobody sees it. Check, and
-- say so in the log of the node that is actually broken.
function dcsbot.jshRefuelLoad(json)
    local path = lfs.writedir():gsub('\\', '/') .. 'Scripts/net/DCSServerBot/jsh_refuel/mission.lua'
    local file = io.open(path, 'r')
    if not file then
        log.write('DCSServerBot', log.ERROR, 'Refuel: mission.lua NOT FOUND at ' .. path ..
            ' - jsh_refuel is not in the opt_plugins of this node, or the bot has not' ..
            ' been restarted since it was added. No refuelling will be paid.')
        return
    end
    file:close()
    log.write('DCSServerBot', log.DEBUG, 'Refuel: loading ' .. path)
    net.dostring_in('mission', 'a_do_script("dofile(\\"' .. path .. '\\")")')
end

-- Runs a Lua snippet inside the mission scripting environment.
function dcsbot.jshRefuelRun(json)
    log.write('DCSServerBot', log.DEBUG, 'Refuel: jshRefuelRun()')
    net.dostring_in('mission', 'a_do_script(' .. string.format('%q', json.lua) .. ')')
end
