import importlib.util
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('compat',ROOT/'tools/patch_awardautomation.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

def test_patch_exact_target_and_idempotence(tmp_path):
    target=tmp_path/'plugins/awardautomation/commands.py';target.parent.mkdir(parents=True)
    text='''class Plugin:
    async def _hardcore_streak(self):
        sql = """SELECT hardcore_at_start, hardcore_revoked, gross_half_units
                  FROM hardcore_sessions"""
    async def other(self):
        return 'untouched'
'''
    target.write_text(text)
    assert m.patch(tmp_path).startswith('Patched')
    assert m.NEW in target.read_text()
    assert "return 'untouched'" in target.read_text()
    assert target.with_suffix('.py.pre-hardcore-2.1').read_text()==text
    first=target.read_bytes();m.patch(tmp_path);assert target.read_bytes()==first
