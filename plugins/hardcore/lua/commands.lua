local utils = require('DCSServerBotUtils')
dcsbot.hardcoreConfig = { enabled = false }
function dcsbot.configureHardcore(json)
    dcsbot.hardcoreConfig = json.config or { enabled = false }
    if dcsbot.hardcoreRefresh then dcsbot.hardcoreRefresh() end
end
