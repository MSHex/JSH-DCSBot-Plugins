-- All geometry and pre-incident evidence are obtained in the mission environment.
-- No core bot or user mission file is modified.
if dcsbot.hardcore then return end
local h = {actors={}, units={}, generations={}, seq=0, cfg={}, geometry={}}
dcsbot.hardcore = h
local function safe(fn) local ok,v=pcall(fn); if ok then return v end end
local function copy(t) local o={}; for k,v in pairs(t or {}) do o[k]=v end; return o end
local function now() return timer.getTime() end
local function send(actor, action, data)
    local msg=copy(data); h.seq=h.seq+1
    msg.command='onHardcoreTelemetry'; msg.epoch=h.epoch; msg.seq=h.seq
    msg.ucid=actor.ucid; msg.name=actor.name; msg.id=actor.id; msg.action=action
    dcsbot.sendBotTable(msg)
end
local function dist2(a,b) return (a.x-b.x)^2+(a.z-b.z)^2 end
local function cross(o,a,b) return (a.x-o.x)*(b.z-o.z)-(a.z-o.z)*(b.x-o.x) end
local function hull(points)
    table.sort(points,function(a,b) return a.x<b.x or (a.x==b.x and a.z<b.z) end)
    if #points<3 then return points end
    local lower,upper={},{}
    for _,p in ipairs(points) do
        while #lower>=2 and cross(lower[#lower-1],lower[#lower],p)<=0 do table.remove(lower) end
        lower[#lower+1]=p
    end
    for i=#points,1,-1 do local p=points[i]
        while #upper>=2 and cross(upper[#upper-1],upper[#upper],p)<=0 do table.remove(upper) end
        upper[#upper+1]=p
    end
    table.remove(lower); table.remove(upper)
    for _,p in ipairs(upper) do lower[#lower+1]=p end
    return lower
end
local function inside(points,p,margin)
    if #points==0 then return false end
    if #points==1 then return dist2(points[1],p)<=margin^2 end
    local all=true
    for i,a in ipairs(points) do
        local b=points[i % #points+1]
        if cross(a,b,p)<0 then all=false end
        local dx,dz=b.x-a.x,b.z-a.z
        local len=dx*dx+dz*dz
        local t=len>0 and math.max(0,math.min(1,((p.x-a.x)*dx+(p.z-a.z)*dz)/len)) or 0
        if dist2(p,{x=a.x+t*dx,z=a.z+t*dz})<=margin^2 then return true end
    end
    return #points>=3 and all
end
local function airfieldGeometry(base)
    local name=base:getName()
    if h.geometry[name] then return h.geometry[name] end
    local points={}
    for _,r in pairs(safe(function() return base:getRunways() end) or {}) do
        if r.position and r.length and r.width and r.course then
            local c,s=math.cos(-r.course),math.sin(-r.course)
            for _,x in ipairs({-r.length/2,r.length/2}) do
                for _,z in ipairs({-r.width/2,r.width/2}) do
                    points[#points+1]={x=r.position.x+x*c-z*s,z=r.position.z+x*s+z*c}
                end
            end
        end
    end
    for _,p in pairs(safe(function() return base:getParking(false) end) or {}) do
        if p.vTerminalPos then points[#points+1]=p.vTerminalPos end
    end
    h.geometry[name]=hull(points)
    return h.geometry[name]
end
local function friendlyPlace(unit, side)
    local point=unit:getPoint()
    for _,base in pairs(world.getAirbases()) do
        if base:getCoalition()==side and side~=0 then
            local name=base:getName()
            local category=base:getDesc().category
            local hit=false
            if category==Airbase.Category.SHIP then
                local ship=base:getUnit()
                if ship and ship:isExist() then
                    local pose,box=ship:getPosition(),ship:getDesc().box
                    if box then
                        local d={x=point.x-pose.p.x,y=point.y-pose.p.y,z=point.z-pose.p.z}
                        local function dot(v) return d.x*v.x+d.y*v.y+d.z*v.z end
                        local x,y,z=dot(pose.x),dot(pose.y),dot(pose.z)
                        -- Ground contact + ship-local footprint + elevation above water.
                        -- This excludes aircraft swimming/parked beside the ship.
                        hit=x>=box.min.x-5 and x<=box.max.x+5 and z>=box.min.z-5 and z<=box.max.z+5
                            and y>=5 and y<=box.max.y+10
                    end
                end
            else
                local zones=h.cfg.protection_zones or {}
                local zoneName=zones[name]
                if zoneName then
                    local zone=trigger.misc.getZone(zoneName)
                    hit=zone and dist2(point,zone.point)<=zone.radius^2 or false
                elseif category==Airbase.Category.HELIPAD then
                    hit=dist2(point,base:getPoint())<=(h.cfg.farp_radius_m or 150)^2
                elseif category==Airbase.Category.AIRDROME then
                    hit=inside(airfieldGeometry(base),point,h.cfg.airfield_margin_m or 75)
                end
            end
            if hit then return name end
        end
    end
end
local function role(actor)
    local allowed=h.cfg.support_roles or {'instructor','artillery_commander','forward_observer','game-master','gamemaster'}
    for _,r in ipairs(allowed) do if actor.unit_type==r then return 'support' end end
    if actor.side~=0 and actor.slot and actor.slot>0 and actor.unit_name then return 'aircraft' end
    return 'spectator'
end
local function unitState(actor)
    local unit=actor.unit_name and Unit.getByName(actor.unit_name)
    if not unit or not safe(function() return unit:isExist() end) then return nil end
    local id=unit:getID()
    local gen=h.generations[actor.unit_name] or 0
    local key=actor.unit_name .. ':' .. tostring(id) .. ':' .. tostring(gen)
    local rec=h.units[key]
    if not rec then
        rec={key=key, name=actor.unit_name, id=id, members={}, state={}, time=now()}
        h.units[key]=rec
    end
    rec.members[actor.ucid]=copy(actor)
    if not rec.terminal and not rec.pending and (unit:getLife() or 0)>1 then
        local airborne=unit:inAir()
        local place=not airborne and friendlyPlace(unit,actor.side) or nil
        rec.state={airborne=airborne,protected=not airborne and place~=nil,place=place}
        rec.time=now()
    end
    return rec
end
local function evidence(rec)
    if rec and now()-rec.time <= (h.cfg.evidence_max_age_seconds or 5) then return copy(rec.state) end
    return {reason='No fresh pre-incident evidence'}
end
local function flush(rec)
    if not rec.pending or rec.terminal then return end
    local pending=rec.pending
    rec.terminal=true; rec.pending=nil
    for ucid,actor in pairs(pending.members) do
        local data=copy(pending.evidence)
        data.kind=pending.kind; data.spawn=rec.key
        data.current=h.actors[ucid] and h.actors[ucid].spawn==rec.key or false
        send(actor,'incident',data)
    end
end
local function reslot(actor,rec)
    if not rec or rec.terminal then return end
    if rec.pending then
        -- An explicit, pre-fatal slot-change attempt followed by an actual slot
        -- change identifies DCS's destruction of the abandoned unit as a reslot.
        -- An attempt alone never changes financial state.
        local attempt=rec.attempts and rec.attempts[actor.ucid]
        if rec.pending.kind=='death' and attempt and now()-attempt.time<=5 then
            local data=copy(attempt.evidence); data.kind='reslot'; data.spawn=rec.key
            send(actor,'incident',data)
            rec.pending.members[actor.ucid]=nil
            rec.members[actor.ucid]=nil
        end
        return
    end
    local data=evidence(rec); data.kind='reslot'; data.spawn=rec.key
    send(actor,'incident',data)
    rec.members[actor.ucid]=nil
end
function h.sync(json)
    local payload=net.json2lua(json)
    h.cfg=payload.config or {}; h.epoch=payload.epoch
    if not h.cfg.enabled then return end
    local seen={}
    for _,incoming in ipairs(payload.roster) do
        local actor=copy(incoming); actor.role=role(actor)
        local previous=h.actors[actor.ucid]
        local rec=actor.role=='aircraft' and safe(function() return unitState(actor) end) or nil
        actor.spawn=rec and rec.key or nil
        seen[actor.ucid]=true
        if rec and payload.attempt_id==actor.id and not rec.pending and not rec.terminal then
            rec.attempts=rec.attempts or {}
            rec.attempts[actor.ucid]={time=now(),evidence=evidence(rec)}
        end
        -- Do not manufacture a reslot when a dead/momentarily absent unit disappears.
        local changed=previous and (previous.slot~=actor.slot or previous.unit_name~=actor.unit_name
            or previous.sub_slot~=actor.sub_slot or previous.role~=actor.role
            or (previous.spawn and actor.spawn and previous.spawn~=actor.spawn))
        if changed and previous.role=='aircraft' then reslot(previous,h.units[previous.spawn]) end
        if not actor.spawn and previous and not changed then actor.spawn=previous.spawn end
        h.actors[actor.ucid]=actor
        local data=evidence(rec); data.role=actor.role; data.spawn=actor.spawn
        if actor.role=='aircraft' and (not rec or rec.terminal or rec.pending) then data.role='spectator' end
        local signature=data.role..':'..tostring(data.spawn)..':'..tostring(data.airborne)..':'..tostring(data.protected)..':'..tostring(data.place)
        actor.signature=signature
        actor.sent_at=previous and previous.sent_at or -100
        if not previous or signature~=previous.signature or now()-actor.sent_at>=5 then
            send(actor,'state',data); actor.sent_at=now()
        end
    end
    for ucid,actor in pairs(h.actors) do
        if not seen[ucid] then
            -- The Python adapter gives pending death/ejection messages time to arrive.
            if actor.spawn and h.units[actor.spawn] then h.units[actor.spawn].members[ucid]=nil end
            h.actors[ucid]=nil
        end
    end
end
function h.flushAll()
    for _,rec in pairs(h.units) do flush(rec) end
end
local handler={}
function handler:onEvent(event)
    local ok,err=pcall(function()
        local unit=event.initiator
        local name=unit and safe(function() return unit:getName() end)
        if not name then return end
        if event.id==world.event.S_EVENT_BIRTH then
            h.generations[name]=(h.generations[name] or 0)+1
            return
        end
        local kind
        if event.id==world.event.S_EVENT_EJECTION then kind='ejection'
        elseif event.id==world.event.S_EVENT_CRASH or event.id==world.event.S_EVENT_PILOT_DEAD then kind='death' end
        local id=safe(function() return unit:getID() end)
        local key=name..':'..tostring(id)..':'..tostring(h.generations[name] or 0)
        local rec=h.units[key]
        if not rec or rec.terminal then return end
        if event.id==world.event.S_EVENT_TAKEOFF then
            rec.state={airborne=true,protected=false}; rec.time=now()
        elseif event.id==world.event.S_EVENT_LAND then
            for _,actor in pairs(rec.members) do unitState(actor); break end
        elseif kind then
            if not rec.pending then
                rec.pending={kind=kind,evidence=evidence(rec),members=copy(rec.members)}
                timer.scheduleFunction(function() flush(rec); return nil end,nil,now()+(h.cfg.incident_merge_seconds or 3))
            elseif kind=='ejection' then
                -- Ejection, wreck crash and pilot-dead notifications describe one incident.
                rec.pending.kind='ejection'
            end
        end
    end)
    if not ok then env.warning('Hardcore telemetry event error: '..tostring(err)) end
end
world.addEventHandler(handler)
env.info('DCSServerBot Hardcore 2.1 mission telemetry loaded')
