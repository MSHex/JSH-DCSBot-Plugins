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

-- Air-to-air refuelling: measured by sampling fuel, not by DCS's refuel events.
--
-- The event-driven version this replaces could never pay anyone, and the logs
-- say why. Across a full evening on the Foothold server, DCS fired 13
-- S_EVENT_REFUELING events and named the KC-135 as the initiator in every
-- single one; of 14 stop events, exactly one named the human. So the receiver
-- never appears on the start event, the old handler never recorded a baseline,
-- and the one stop event it could read had nothing to settle against. DCS also
-- delivered that stop event twice at the same timestamp.
--
-- Sampling sidesteps all of it. Fuel going up while airborne is a refuelling,
-- whoever DCS decides to name.
if not jsh_credits.fuelWatch then

    -- Drop the old event handler if this mission still has one loaded, so a
    -- server that had the previous version injected is not left with a dead
    -- handler sitting on the event bus.
    if jsh_credits.refuelHandler then
        pcall(world.removeEventHandler, jsh_credits.refuelHandler)
        jsh_credits.refuelHandler = nil
        jsh_credits.refuelStart = nil
    end

    jsh_credits.fuelWatch = {}   -- unit name -> sampling state

    local KG_TO_LBS = 2.20462262185
    local SAMPLE_SECONDS = 5
    -- Below this a change is floating-point noise rather than fuel. On an F-16
    -- this is worth about three pounds.
    local EPSILON = 0.0005
    -- Quiet samples that mean the boom is out and the session can be settled.
    local IDLE_SAMPLES = 2

    local function flush(state)
        local lbs = math.floor(state.pending)
        state.pending = 0
        state.idle = 0
        if lbs <= 0 then
            return
        end
        dcsbot.sendBotTable({
            command  = 'jshCreditsRefuel',
            name     = state.name,
            lbs      = lbs,
            aircraft = state.aircraft
        })
    end

    --- One pass over every player aircraft.
    --
    -- The whole session is accumulated and sent as a single total. That is not
    -- tidiness: the bot applies a minimum (minimum_lbs, 500 by default) before
    -- paying anything, so a drip of five-second deltas would each fall under it
    -- and the pilot would be paid nothing at all.
    local function sample()
        local seen = {}

        for _, side in pairs({ coalition.side.RED, coalition.side.BLUE }) do
            for _, unit in pairs(coalition.getPlayers(side) or {}) do
                pcall(function()
                    if not unit:isExist() then
                        return
                    end
                    local player = unit:getPlayerName()
                    if not player or player == '' then
                        return
                    end
                    local unit_name = unit:getName()
                    seen[unit_name] = true

                    local fuel = unit:getFuel()
                    local desc = unit:getDesc() or {}
                    local massMax = desc.fuelMassMax or 0
                    if type(fuel) ~= 'number' or massMax <= 0 then
                        return
                    end

                    local state = jsh_credits.fuelWatch[unit_name]
                    if not state or state.name ~= player then
                        -- First sight of this airframe, or someone else is in
                        -- it now. Record a baseline and pay nothing for it.
                        jsh_credits.fuelWatch[unit_name] = {
                            name = player, fuel = fuel, pending = 0, idle = 0,
                            aircraft = unit:getTypeName() or ''
                        }
                        return
                    end

                    local delta = fuel - state.fuel
                    state.fuel = fuel

                    -- inAir is what keeps ground refuelling and rearming out of
                    -- this. Note there is deliberately no cap at a full
                    -- internal load: getFuel() exceeds 1.0 when external tanks
                    -- are carried, and filling those is paid for like any other
                    -- fuel taken from a tanker.
                    if unit:inAir() and delta > EPSILON then
                        state.pending = state.pending + delta * massMax * KG_TO_LBS
                        state.idle = 0
                    elseif state.pending > 0 then
                        state.idle = state.idle + 1
                        if state.idle >= IDLE_SAMPLES then
                            flush(state)
                        end
                    end
                end)
            end
        end

        -- Anyone who left the slot mid-session: settle whatever they banked.
        for unit_name, state in pairs(jsh_credits.fuelWatch) do
            if not seen[unit_name] then
                if state.pending > 0 then
                    flush(state)
                end
                jsh_credits.fuelWatch[unit_name] = nil
            end
        end
    end

    local function tick(_, time)
        local ok, err = pcall(sample)
        if not ok then
            env.info('jsh_credits: fuel sampler error: ' .. tostring(err))
        end
        return time + SAMPLE_SECONDS
    end

    timer.scheduleFunction(tick, nil, timer.getTime() + SAMPLE_SECONDS)
    env.info('jsh_credits: refuel handler registered (fuel sampler, every '
             .. SAMPLE_SECONDS .. 's)')

end
