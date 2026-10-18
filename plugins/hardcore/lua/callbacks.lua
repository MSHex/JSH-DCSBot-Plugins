local utils = require('DCSServerBotUtils')
local hc = {}
local ready, nextTick = false, 0
local epoch = tostring(os.time()) .. ':' .. tostring(math.random(100000,999999))

function dcsbot.hardcoreRefresh(attemptId)
    if not ready then return end
    local cfg = dcsbot.hardcoreConfig or {}
    if not cfg.enabled then return end
    local roster = {}
    for _, id in ipairs(net.get_player_list()) do
        if id ~= 1 then
            local info = net.get_player_info(id)
            if info and info.ucid then
                local typ, slot, sub = utils.getMulticrewAllParameters(id)
                roster[#roster+1] = {
                    id=id, ucid=info.ucid, name=info.name, side=info.side,
                    unit_type=typ, slot=slot, sub_slot=sub,
                    unit_name=slot and slot > 0 and Sim.getUnitProperty(slot, Sim.UNIT_NAME) or nil
                }
            end
        end
    end
    local payload = net.lua2json({roster=roster, config=cfg, epoch=epoch, attempt_id=attemptId})
    local script = 'if dcsbot.hardcore then dcsbot.hardcore.sync(' .. utils.basicSerialize(payload) .. ') end'
    net.dostring_in('mission', 'a_do_script(' .. utils.basicSerialize(script) .. ')')
end
function hc.onMissionLoadEnd()
    epoch = tostring(os.time()) .. ':' .. tostring(math.random(100000,999999))
    utils.loadScript('DCSServerBot.lua')
    utils.loadScript('hardcore/mission.lua')
    ready, nextTick = true, 0
    dcsbot.hardcoreRefresh()
end
function hc.onSimulationFrame()
    if ready and Sim.getModelTime() >= nextTick then
        nextTick = Sim.getModelTime() + 1
        dcsbot.hardcoreRefresh()
    end
end
function hc.onPlayerTryChangeSlot(id, side, slot)
    -- Sample OLD aircraft before DCS removes it. Never deny or count an attempt.
    dcsbot.hardcoreRefresh(id)
end
function hc.onPlayerChangeSlot(id) dcsbot.hardcoreRefresh() end
function hc.onPlayerStart(id) dcsbot.hardcoreRefresh() end
function hc.onSimulationStop()
    local script='if dcsbot.hardcore then dcsbot.hardcore.flushAll() end'
    net.dostring_in('mission', 'a_do_script(' .. utils.basicSerialize(script) .. ')')
    ready = false
end
Sim.setUserCallbacks(hc)
