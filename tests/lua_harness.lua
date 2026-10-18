clock=0; messages={}; scheduled={}; units={}; bases={}; handlers={}
dcsbot={sendBotTable=function(m) messages[#messages+1]=m end}
net={json2lua=function(x) return x end}
env={info=function() end,warning=function(m) error(m) end}
Unit={getByName=function(n) return units[n] end}
Airbase={Category={AIRDROME=0,HELIPAD=1,SHIP=2}}
trigger={misc={getZone=function() return nil end}}
world={event={S_EVENT_BIRTH=1,S_EVENT_TAKEOFF=2,S_EVENT_LAND=3,S_EVENT_CRASH=4,S_EVENT_PILOT_DEAD=5,S_EVENT_EJECTION=6},
    getAirbases=function() return bases end,addEventHandler=function(h) handlers[#handlers+1]=h end}
timer={getTime=function() return clock end,scheduleFunction=function(f,arg,t) scheduled[#scheduled+1]={f=f,arg=arg,t=t} end}
function advance(seconds)
    clock=clock+seconds
    local remaining={}
    for _,s in ipairs(scheduled) do if s.t<=clock then s.f(s.arg,s.t) else remaining[#remaining+1]=s end end
    scheduled=remaining
end
function fire(id,unit) for _,h in ipairs(handlers) do h:onEvent({id=id,initiator=unit,time=clock}) end end
function aircraft(name,id,x,z,air)
    local u={name=name,id=id,point={x=x,y=0,z=z},air=air,life=100,exists=true}
    function u:getName() return self.name end
    function u:getID() return self.id end
    function u:isExist() return self.exists end
    function u:getLife() return self.life end
    function u:getPoint() return self.point end
    function u:inAir() return self.air end
    units[name]=u; return u
end
function airbase(name,side,category,x,z)
    local b={name=name,side=side,category=category,point={x=x,y=0,z=z}}
    function b:getName() return self.name end
    function b:getCoalition() return self.side end
    function b:getDesc() return {category=self.category} end
    function b:getPoint() return self.point end
    function b:getRunways() return {{position=self.point,length=2000,width=60,course=0}} end
    function b:getParking() return {{vTerminalPos={x=self.point.x,z=self.point.z+100}}} end
    function b:getUnit() return self.ship end
    bases[#bases+1]=b; return b
end
function roster(typ,slot,name,ucid)
    return {id=2,ucid=ucid or 'u',name='Pilot',side=2,unit_type=typ or 'F-16C',slot=slot or 1,sub_slot=0,unit_name=name or 'jet'}
end
function sync(roster,extra)
    local cfg=extra or {}; cfg.enabled=true
    dcsbot.hardcore.sync({epoch='mission1',config=cfg,roster=roster})
end
function incidents()
    local a={}; for _,m in ipairs(messages) do if m.action=='incident' then a[#a+1]=m end end; return a
end
