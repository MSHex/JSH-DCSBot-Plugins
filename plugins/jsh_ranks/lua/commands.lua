-- Hook environment: commands sent from the bot (Python) to DCS.
local base   = _G
local dcsbot = base.dcsbot

-- Runs a Lua snippet inside the mission scripting environment.
function dcsbot.jshRanksRun(json)
    log.write('DCSServerBot', log.DEBUG, 'Ranks: jshRanksRun()')
    net.dostring_in('mission', 'a_do_script(' .. string.format('%q', json.lua) .. ')')
end
