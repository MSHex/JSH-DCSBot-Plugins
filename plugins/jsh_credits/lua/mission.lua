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
