from pathlib import Path
from lupa import LuaRuntime
import pytest
ROOT=Path(__file__).resolve().parents[1]
@pytest.fixture
def lua():
    l=LuaRuntime(unpack_returned_tuples=True)
    l.execute((ROOT/'tests/lua_harness.lua').read_text())
    l.execute((ROOT/'plugins/hardcore/lua/mission.lua').read_text())
    return l

def test_friendly_taxi_death_protected(lua):
    lua.execute("b=airbase('Friendly',2,0,0,0); u=aircraft('jet',10,0,0,false); sync({roster()}); u.life=0; fire(4,u); fire(5,u); advance(4)")
    assert lua.eval('#incidents()')==1
    assert lua.eval('incidents()[1].protected') is True
    assert lua.eval('incidents()[1].kind')=='death'

@pytest.mark.parametrize('side', [0,1])
def test_neutral_enemy_unprotected(lua,side):
    lua.execute(f"airbase('Other',{side},0,0,0); u=aircraft('jet',10,0,0,false); sync({{roster()}}); fire(5,u); advance(4)")
    assert lua.eval('incidents()[1].protected') is False

def test_offfield_and_capture(lua):
    lua.execute("b=airbase('Base',0,0,0,0); u=aircraft('jet',10,0,0,false); sync({roster()})")
    assert lua.eval('messages[#messages].protected') is False
    lua.execute("b.side=2; sync({roster()})")
    assert lua.eval('messages[#messages].protected') is True
    lua.execute("u.point.x=6000; sync({roster()})")
    assert lua.eval('messages[#messages].protected') is False

def test_eject_crash_death_one_incident_regardless_order(lua):
    lua.execute("u=aircraft('jet',10,0,0,true); sync({roster()}); fire(4,u); fire(6,u); fire(5,u); advance(4); fire(5,u)")
    assert lua.eval('#incidents()')==1
    assert lua.eval('incidents()[1].kind')=='ejection'
    assert lua.eval('incidents()[1].protected') is False

def test_ground_impact_not_protection(lua):
    lua.execute("airbase('Friendly',2,0,0,0); u=aircraft('jet',10,0,0,true); sync({roster()}); u.air=false; u.life=0; fire(4,u); advance(4)")
    assert lua.eval('incidents()[1].protected') is False

def test_safe_reslot_after_missed_landing_callback(lua):
    lua.execute("airbase('Friendly',2,0,0,0); u=aircraft('jet',10,0,0,true); sync({roster()}); u.air=false; sync({roster()}); aircraft('jet2',11,0,0,false); sync({roster('F-18',2,'jet2')})")
    assert lua.eval('#incidents()')==1
    assert lua.eval('incidents()[1].kind')=='reslot'
    assert lua.eval('incidents()[1].protected') is True

def test_unprotected_ground_reslot(lua):
    lua.execute("u=aircraft('jet',10,9000,0,false); sync({roster()}); sync({roster('instructor',-1,'')})")
    assert lua.eval('incidents()[1].protected') is False

def test_late_old_death_does_not_close_new_clock(lua):
    lua.execute("u=aircraft('jet',10,0,0,true); sync({roster()}); fire(6,u); aircraft('jet2',11,0,0,false); sync({roster('F-18',2,'jet2')}); advance(4)")
    assert lua.eval('#incidents()')==1
    assert lua.eval('incidents()[1].current') is False

def test_new_spawn_gets_new_incident(lua):
    lua.execute("u=aircraft('jet',10,0,0,true); sync({roster()}); fire(6,u); advance(4); fire(1,u); sync({roster()}); fire(6,u); advance(4)")
    assert lua.eval('#incidents()')==2
    assert lua.eval('incidents()[1].spawn ~= incidents()[2].spawn') is True

def test_stale_evidence_no_inferred_protection(lua):
    lua.execute("u=aircraft('jet',10,0,0,true); sync({roster()}); advance(10); fire(5,u); advance(4)")
    assert lua.eval('incidents()[1].protected') is None

def test_multicrew(lua):
    lua.execute("u=aircraft('jet',10,0,0,true); a=roster(); b=roster(nil,nil,nil,'crew'); b.sub_slot=1; sync({a,b}); fire(6,u); fire(4,u); advance(4)")
    assert lua.eval('#incidents()')==2
    assert lua.eval('incidents()[1].ucid ~= incidents()[2].ucid') is True

def test_support_role_occupancy(lua):
    lua.execute("sync({roster('instructor',-1,'')})")
    assert lua.eval('messages[#messages].role')=='support'
    lua.execute("sync({roster('observer',-1,'')})")
    assert lua.eval('messages[#messages].role')=='spectator'

def test_farp(lua):
    lua.execute("airbase('FARP',2,1,0,0); u=aircraft('jet',10,100,0,false); sync({roster()})")
    assert lua.eval('messages[#messages].protected') is True
    lua.execute("u.point.x=200; sync({roster()})")
    assert lua.eval('messages[#messages].protected') is False

def test_carrier_deck_not_nearby_sea(lua):
    lua.execute("""
    b=airbase('Carrier',2,2,0,0)
    b.ship={isExist=function() return true end,
      getPosition=function() return {p={x=0,y=0,z=0},x={x=1,y=0,z=0},y={x=0,y=1,z=0},z={x=0,y=0,z=1}} end,
      getDesc=function() return {box={min={x=-150,z=-25},max={x=150,y=55,z=25}}} end}
    u=aircraft('jet',10,0,0,false); u.point.y=20; sync({roster()})
    """)
    assert lua.eval('messages[#messages].protected') is True
    lua.execute("u.point.y=0; sync({roster()})")
    assert lua.eval('messages[#messages].protected') is False
    lua.execute("u.point.y=20; u.air=true; sync({roster()})")
    assert lua.eval('messages[#messages].protected') is False

def test_explicit_ground_reslot_destruction_reported_as_reslot(lua):
    lua.execute("""
    airbase('Friendly',2,0,0,0); u=aircraft('jet',10,0,0,false); sync({roster()})
    dcsbot.hardcore.sync({epoch='mission1',config={enabled=true},roster={roster()},attempt_id=2})
    u.life=0; fire(4,u)
    aircraft('jet2',11,0,0,false); sync({roster('F-18',2,'jet2')}); advance(4)
    """)
    assert lua.eval('#incidents()')==1
    assert lua.eval('incidents()[1].kind')=='reslot'
    assert lua.eval('incidents()[1].protected') is True

def test_failed_slot_attempt_not_a_loss(lua):
    lua.execute("""
    u=aircraft('jet',10,0,0,true); sync({roster()})
    dcsbot.hardcore.sync({epoch='mission1',config={enabled=true},roster={roster()},attempt_id=2})
    sync({roster()}); advance(4)
    """)
    assert lua.eval('#incidents()')==0

def test_mission_stop_flushes_ejection(lua):
    lua.execute("u=aircraft('jet',10,0,0,true); sync({roster()}); fire(6,u); dcsbot.hardcore.flushAll()")
    assert lua.eval('#incidents()')==1
    assert lua.eval('incidents()[1].kind')=='ejection'
