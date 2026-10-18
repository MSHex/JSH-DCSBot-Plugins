import importlib.util
import sys
from pathlib import Path
import pytest
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('hardcore_policy',ROOT/'plugins/hardcore/policy.py')
p=importlib.util.module_from_spec(spec);sys.modules[spec.name]=p;spec.loader.exec_module(p)

@pytest.mark.parametrize('kind', ['death','ejection','reslot'])
def test_friendly_protection(kind):
    assert p.loss_decision(kind,True,True,0)==(False,False,0)
    assert p.loss_decision(kind,None,True,0)==(False,False,0)

def test_allowance_and_direct_death():
    assert p.loss_decision('ejection',False,True,0)==(True,False,1)
    assert p.loss_decision('ejection',False,True,1)==(True,True,2)
    assert p.loss_decision('death',False,True,0)==(True,True,0)
    assert p.loss_decision('death',False,True,1)==(True,True,1)
    assert p.loss_decision('reslot',False,True,0)==(True,True,0)

def test_support_no_bonus_and_no_flight_penalty():
    e=p.earnings(900,900,15,25,37.5,True,20)
    assert (e.aircraft_half,e.support_half,e.net_half)==(75,50,110)
    assert p.earnings(0,900,15,25,37.5,True,90).net_half==50

def test_revocation_reprices_aircraft():
    assert p.earnings(900,900,15,25,37.5,False,50).net_half==75

def test_partial_role_blocks_not_repriced():
    assert p.earnings(450,450,15,25,37.5,True,0).net_half==0
    assert p.earnings(900,900,15,25,37.5,True,0).net_half==125

@pytest.mark.parametrize('role', ['instructor','artillery_commander','forward_observer'])
def test_support_minus_one_slots(role):
    assert p.role_kind(role,-1,2)=='support'
    assert p.role_kind('observer',-1,2)=='spectator'
