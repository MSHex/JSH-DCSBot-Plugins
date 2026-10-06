-- Hook environment: commands sent from the bot (Python) to DCS.
local base   = _G
local dcsbot = base.dcsbot

-- Loads mission.lua into the running mission. Sent by the bot only for
-- servers where the plugin is enabled in jsh_csar.yaml.
function dcsbot.jshCsarLoad(json)
    log.write('DCSServerBot', log.DEBUG, 'JSH CSAR: jshCsarLoad()')
    local path = lfs.writedir():gsub('\\', '/') .. 'Scripts/net/DCSServerBot/jsh_csar/mission.lua'
    net.dostring_in('mission', 'a_do_script("dofile(\\"' .. path .. '\\")")')
end

-- Runs a Lua snippet inside the mission scripting environment.
function dcsbot.jshCsarRun(json)
    log.write('DCSServerBot', log.DEBUG, 'JSH CSAR: jshCsarRun()')
    net.dostring_in('mission', 'a_do_script(' .. string.format('%q', json.lua) .. ')')
end
