-- JSH Credits: landing hook.
--
-- Injected by the jsh_credits plugin at mission load via do_script. Reports a
-- landing to the bot only when the aircraft touched down at an airbase, FARP or
-- ship belonging to the pilot's own coalition. Landing in a field gives
-- event.place == nil and is ignored, as is landing at an enemy or neutral field.
--
-- Ownership is read live from the mission, so a base captured mid-campaign is
-- judged by who holds it right now.

jsh_credits = jsh_credits or {}

if not jsh_credits.handler then

    jsh_credits.handler = {}

    function jsh_credits.handler:onEvent(event)
        if not event or event.id ~= world.event.S_EVENT_LAND then
            return
        end

        local ok, err = pcall(function()
            local unit = event.initiator
            if not unit or not unit.getPlayerName then
                return
            end

            local name = unit:getPlayerName()
            if not name then
                return  -- AI landing
            end

            local place = event.place
            if not place then
                return  -- off-field landing
            end

            -- a destroyed or invalid place object has no coalition
            if not place.getCoalition then
                return
            end

            local place_coalition = place:getCoalition()
            local unit_coalition = unit:getCoalition()
            if place_coalition ~= unit_coalition then
                return  -- enemy or neutral field
            end

            local place_name = ''
            if place.getName then
                place_name = place:getName() or ''
            end

            dcsbot.sendBotTable({
                command = 'jshCreditsLanding',
                name    = name,
                place   = place_name
            })
        end)

        if not ok then
            env.info('jsh_credits: landing handler error: ' .. tostring(err))
        end
    end

    world.addEventHandler(jsh_credits.handler)
    env.info('jsh_credits: landing handler registered')

end

-- Air-to-air refuelling.
--
-- DCS names the RECEIVING aircraft as the initiator of both events, so the
-- pilot taking the fuel is the one measured. getFuel() is a fraction of
-- internal capacity and getDesc().fuelMassMax is that capacity in kilograms, so
-- the change between connect and disconnect converts straight to pounds.
--
-- Two known approximations, neither of which matters for an economy: fuel in
-- external tanks sits outside fuelMassMax, and the aircraft burns while it
-- drinks. Both make the figure conservative.
--
-- The bot decides what it is worth. Everything here just measures.

if not jsh_credits.refuelHandler then

    jsh_credits.refuelStart = {}    -- unit name -> fuel state when the boom went in

    local KG_TO_LBS = 2.20462262185

    local function fuelState(unit)
        local ok, state = pcall(function()
            local desc = unit:getDesc() or {}
            return { fuel = unit:getFuel(), massMax = desc.fuelMassMax or 0 }
        end)
        if ok and state and type(state.fuel) == 'number' and state.massMax > 0 then
            return state
        end
        return nil
    end

    jsh_credits.refuelHandler = {}

    function jsh_credits.refuelHandler:onEvent(event)
        if not event then
            return
        end
        local id = event.id
        if id ~= world.event.S_EVENT_REFUELING and id ~= world.event.S_EVENT_REFUELING_STOP then
            return
        end

        local ok, err = pcall(function()
            local unit = event.initiator
            if not unit or not unit.getPlayerName then
                return
            end
            local name = unit:getPlayerName()
            if not name or name == '' then
                return  -- AI receiver
            end
            local unit_name = unit:getName()

            if id == world.event.S_EVENT_REFUELING then
                local state = fuelState(unit)
                if state then
                    jsh_credits.refuelStart[unit_name] =
                        { name = name, fuel = state.fuel, massMax = state.massMax }
                end
                return
            end

            -- S_EVENT_REFUELING_STOP: the boom came out, so settle up.
            local start = jsh_credits.refuelStart[unit_name]
            jsh_credits.refuelStart[unit_name] = nil
            if not start or start.name ~= name then
                return
            end
            local state = fuelState(unit)
            if not state then
                return
            end

            -- Can be zero or negative on a disconnect with no fuel taken.
            local lbs = math.floor((state.fuel - start.fuel) * start.massMax * KG_TO_LBS)
            if lbs <= 0 then
                return
            end

            local aircraft = ''
            pcall(function() aircraft = unit:getTypeName() or '' end)

            dcsbot.sendBotTable({
                command  = 'jshCreditsRefuel',
                name     = name,
                lbs      = lbs,
                aircraft = aircraft
            })
        end)

        if not ok then
            env.info('jsh_credits: refuel handler error: ' .. tostring(err))
        end
    end

    world.addEventHandler(jsh_credits.refuelHandler)
    env.info('jsh_credits: refuel handler registered')

end
