"""Execute the production economy against PostgreSQL compiled to WASM (PGlite).

Set HARDCORE_PGLITE_BRIDGE to the path of tests/pglite_bridge.mjs with its npm
package installed. These tests do not contact a production database.
"""
import asyncio
import importlib.util
import json
import logging
import os
import subprocess
import sys
import types
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
import pytest
ROOT=Path(__file__).resolve().parents[1]
BRIDGE=os.getenv('HARDCORE_PGLITE_BRIDGE')
pytestmark=pytest.mark.skipif(not BRIDGE,reason='PGlite bridge not configured')

class Cursor:
    def __init__(self,db,dicts=False): self.db=db; self.dicts=dicts; self.rows=[]
    async def __aenter__(self): return self
    async def __aexit__(self,*args): pass
    async def execute(self,sql,args=()):
        response=self.db.query(sql,args)
        self.rows=response.get('rows',[])
        for row in self.rows:
            for k,v in row.items():
                if isinstance(v,str) and k.endswith('_at'):
                    row[k]=datetime.fromisoformat(v.replace('Z','+00:00')).replace(tzinfo=None)
        if not self.dicts: self.rows=[tuple(r.values()) for r in self.rows]
        return self
    async def fetchone(self): return self.rows.pop(0) if self.rows else None
    async def fetchall(self): rows=self.rows;self.rows=[];return rows

class Connection:
    def __init__(self,db,transaction=False): self.db=db;self.tx=transaction
    async def __aenter__(self):
        if self.tx: self.db.query('BEGIN')
        return self
    async def __aexit__(self,typ,*args):
        if self.tx:self.db.query('ROLLBACK' if typ else 'COMMIT')
    def transaction(self): return Connection(self.db,True)
    def cursor(self,row_factory=None): return Cursor(self.db,bool(row_factory))
    async def execute(self,sql,args=()): return await Cursor(self.db).execute(sql,args)

class Database:
    def __init__(self):
        self.proc=subprocess.Popen(['node',BRIDGE],stdin=subprocess.PIPE,stdout=subprocess.PIPE,text=True)
    def query(self,sql,args=()): return self.call({'sql':sql,'args':args})
    def call(self,payload):
        self.proc.stdin.write(json.dumps(payload,default=lambda x:x.isoformat()+'Z')+'\n');self.proc.stdin.flush()
        row=json.loads(self.proc.stdout.readline())
        if 'error' in row: raise RuntimeError(row['error'])
        return row
    def connection(self): return Connection(self)
    def close(self): self.proc.stdin.close();self.proc.wait(timeout=10)

@pytest.fixture
def env():
    # Load only the plugin modules; the real bot needs a running service registry.
    core=types.ModuleType('core');core.Server=object
    async def campaign(*args):return 1,'Campaign'
    core.utils=SimpleNamespace(get_running_campaign_async=campaign)
    oldcore=sys.modules.get('core');sys.modules['core']=core
    pkg=types.ModuleType('hc_test');pkg.__path__=[str(ROOT/'plugins/hardcore')];sys.modules['hc_test']=pkg
    for name in ('policy','economy'):
        spec=importlib.util.spec_from_file_location('hc_test.'+name,ROOT/f'plugins/hardcore/{name}.py')
        module=importlib.util.module_from_spec(spec);sys.modules[spec.name]=module;spec.loader.exec_module(module)
    db=Database()
    db.call({'exec':'''CREATE TABLE players(ucid TEXT PRIMARY KEY);
        CREATE TABLE campaigns(id INTEGER PRIMARY KEY);
        CREATE TABLE servers(server_name TEXT PRIMARY KEY);
        CREATE TABLE credits(campaign_id INTEGER, player_ucid TEXT, points INTEGER,PRIMARY KEY(campaign_id,player_ucid));
        CREATE TABLE credits_log(campaign_id INTEGER,event TEXT,player_ucid TEXT,old_points INTEGER,new_points INTEGER,remark TEXT);
        INSERT INTO players VALUES ('u'); INSERT INTO campaigns VALUES (1); INSERT INTO servers VALUES ('test');
        INSERT INTO credits VALUES (1,'u',1000);'''})
    db.call({'exec':(ROOT/'plugins/hardcore/db/tables.sql').read_text()})
    # Upgrade is repeatable without wiping balances or membership.
    db.call({'exec':(ROOT/'plugins/hardcore/db/update_v2.0.sql').read_text()})
    cfg={'enabled':True,'dm_receipt':False,'buyin_fee':500}
    bot=SimpleNamespace(cogs={},get_player=lambda **kw:None,loop=None)
    plugin=SimpleNamespace(bot=bot,apool=db,log=logging.getLogger('test'),node=None,get_config=lambda s:cfg)
    svc=sys.modules['hc_test.economy'].EconomyService(plugin)
    server=SimpleNamespace(name='test',get_player=lambda **kw:None,get_active_players=lambda:[])
    yield svc,server,db
    db.close()
    if oldcore:sys.modules['core']=oldcore
    else:sys.modules.pop('core',None)

def run(coro):return asyncio.run(coro)
def state(seq=1,role='aircraft'):
    return {'epoch':'m','seq':seq,'role':role,'airborne':True,'protected':False}
def incident(seq=2,kind='ejection',spawn='a',protected=False):
    return {'epoch':'m','seq':seq,'kind':kind,'spawn':spawn,'protected':protected,'current':True}

def test_allowance_survives_settlement_and_second_ejection_revokes(env):
    svc,server,db=env
    run(svc.set_hardcore('u',True,'TEST'))
    run(svc.observe(server,'u','Pilot',state()))
    run(svc.record_incident(server,'u',incident()))
    assert run(svc.is_hardcore('u'))
    # Duplicate notification and later wreck death must not consume allowance.
    assert not run(svc.record_incident(server,'u',incident(seq=3,kind='death')))
    db.query("UPDATE hardcore_sessions SET aircraft_seconds=900,eligible_seconds=900")
    first=run(svc.settle(server,'u'))
    assert (first.deaths,first.paid_credits,first.hardcore_revoked)==(1,30,False)
    assert run(svc.settle(server,'u')) is None
    run(svc.observe(server,'u','Pilot',state(4)))
    run(svc.record_incident(server,'u',incident(5,spawn='b')))
    assert not run(svc.is_hardcore('u'))
    assert run(svc.status_for(server,'u'))['ejections_used']==2

def test_direct_death_immediate_and_ground_death_ignored(env):
    svc,server,db=env
    run(svc.set_hardcore('u',True,'TEST'))
    run(svc.observe(server,'u','Pilot',state()))
    run(svc.record_incident(server,'u',incident(2,'death',protected=True)))
    assert run(svc.is_hardcore('u'))
    run(svc.record_incident(server,'u',incident(3,'death',spawn='b')))
    assert not run(svc.is_hardcore('u'))
    assert run(svc.status_for(server,'u'))['session']['deaths']==1

def test_support_only_no_bonus_and_mixed_revocation(env):
    svc,server,db=env
    run(svc.set_hardcore('u',True,'TEST'))
    run(svc.observe(server,'u','Pilot',state(role='support')))
    db.query("UPDATE hardcore_sessions SET support_seconds=900,eligible_seconds=900,segment_started_at=NULL")
    s=run(svc.settle(server,'u'))
    assert (s.paid_credits,s.support_half,s.aircraft_half)==(25,50,0)
    run(svc.observe(server,'u','Pilot',state(2)))
    run(svc.record_incident(server,'u',incident(3,'death')))
    db.query("UPDATE hardcore_sessions SET aircraft_seconds=900,support_seconds=900,eligible_seconds=1800 WHERE NOT settled")
    s=run(svc.settle(server,'u'))
    assert s.paid_credits==45 # 25 aircraft -20%, plus 25 support

def test_role_switches_resume_clock_and_stale_state_ignored(env):
    svc,server,db=env
    run(svc.observe(server,'u','Pilot',state()))
    db.query("UPDATE hardcore_sessions SET segment_started_at=timezone('utc',clock_timestamp())-interval '15 minutes'")
    run(svc.observe(server,'u','Pilot',state(2,'support')))
    s=run(svc.status_for(server,'u'))['session']
    assert s['aircraft_seconds']>=900
    assert s['segment_role']=='support'
    run(svc.observe(server,'u','Pilot',state(1)))
    assert run(svc.status_for(server,'u'))['session']['segment_role']=='support'

def test_reenroll_resets_only_on_new_membership(env):
    svc,server,db=env
    run(svc.set_hardcore('u',True,'TEST'))
    db.query("UPDATE hardcore_status SET ejections_used=1")
    run(svc.set_hardcore('u',True,'ADMIN'))
    assert run(svc.status_for(server,'u'))['ejections_used']==1
    run(svc.set_hardcore('u',False,'ADMIN'))
    ok,_,balance=run(svc.enroll(server,'u'))
    assert ok and balance==500
    assert run(svc.status_for(server,'u'))['ejections_used']==0

def test_restart_does_not_award_downtime(env):
    svc,server,db=env
    run(svc.observe(server,'u','Pilot',state()))
    db.query("UPDATE hardcore_sessions SET segment_started_at=timezone('utc',clock_timestamp())-interval '1 hour',last_seen_at=timezone('utc',clock_timestamp())-interval '45 minutes'")
    run(svc.reconcile_server(server))
    row=db.query('SELECT aircraft_seconds,payout FROM hardcore_sessions')['rows'][0]
    assert 900<=int(row['aircraft_seconds'])<902
    assert row['payout']==25

def test_fractional_carry_and_credit_cap(env):
    svc,server,db=env
    run(svc.set_hardcore('u',True,'TEST'))
    run(svc.observe(server,'u','Pilot',state()))
    db.query("UPDATE hardcore_sessions SET aircraft_seconds=900,eligible_seconds=900,segment_started_at=NULL")
    first=run(svc.settle(server,'u'))
    assert (first.paid_credits,first.carry_half_units)==(37,1)
    run(svc.observe(server,'u','Pilot',state(2)))
    db.query("UPDATE hardcore_sessions SET aircraft_seconds=900,eligible_seconds=900,segment_started_at=NULL WHERE NOT settled")
    second=run(svc.settle(server,'u'))
    assert (second.paid_credits,second.carry_half_units)==(38,0)
    svc.bot.cogs['CreditSystem']=SimpleNamespace(get_config=lambda _: {'max_points':1000})
    run(svc.observe(server,'u','Pilot',state(3)))
    db.query("UPDATE hardcore_sessions SET aircraft_seconds=900,eligible_seconds=900,segment_started_at=NULL WHERE NOT settled")
    capped=run(svc.settle(server,'u'))
    assert capped.paid_credits==0 and capped.old_balance==capped.new_balance==1075

def test_legacy_upgrade_preserves_balances_and_flight_time(env):
    _,_,db=env
    db.call({'exec':'''DROP TABLE hardcore_events; DROP TABLE hardcore_sessions;
       DROP TABLE hardcore_status; DROP TABLE hardcore_wallet_carry;'''})
    old=(ROOT/'plugins/hardcore/db/tables.sql').read_text().split('ALTER TABLE hardcore_status')[0]
    db.call({'exec':old})
    db.call({'exec':'''INSERT INTO hardcore_status(player_ucid,active) VALUES ('u',TRUE);
       INSERT INTO hardcore_sessions(server_name,player_ucid,campaign_id,eligible_seconds,settled,gross_half_units)
       VALUES ('test','u',1,1800,TRUE,150);'''})
    db.call({'exec':(ROOT/'plugins/hardcore/db/update_v2.0.sql').read_text()})
    row=db.query('SELECT aircraft_seconds,support_seconds,aircraft_gross_half_units FROM hardcore_sessions')['rows'][0]
    assert tuple(row.values())==(1800,0,150)
    assert db.query("SELECT active,ejections_used FROM hardcore_status")['rows'][0]=={'active':True,'ejections_used':0}
    assert db.query('SELECT points FROM credits')['rows'][0]['points']==1000

def test_award_query_ignores_support_only_and_keeps_legacy(env):
    svc,server,db=env
    run(svc.set_hardcore('u',True,'TEST'))
    run(svc.observe(server,'u','Pilot',state(role='support')))
    db.query("UPDATE hardcore_sessions SET support_seconds=900,segment_started_at=NULL")
    run(svc.settle(server,'u'))
    row=db.query("SELECT COALESCE((to_jsonb(hardcore_sessions)->>'aircraft_gross_half_units')::bigint,gross_half_units) AS gross_half_units FROM hardcore_sessions")['rows'][0]
    assert row['gross_half_units']==0
