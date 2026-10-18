"""Run from the DCSServerBot root: python tools/patch_awardautomation.py

Makes only the known Hardcore-streak SELECT compatible with separate support
payouts. Refuses to guess if the installed method differs. Backups are kept.
"""
import argparse
from pathlib import Path

OLD='SELECT hardcore_at_start, hardcore_revoked, gross_half_units'
NEW='''SELECT hardcore_at_start, hardcore_revoked,
                   COALESCE((to_jsonb(hardcore_sessions)->>'aircraft_gross_half_units')::bigint,
                            gross_half_units) AS gross_half_units'''

def patch(root):
    target=Path(root)/'plugins/awardautomation/commands.py'
    if not target.exists():
        return 'AwardAutomation is not installed; no change needed.'
    text=target.read_text(encoding='utf-8')
    start=text.find('    async def _hardcore_streak(')
    end=text.find('\n    async def ',start+1)
    if start<0 or end<0:
        raise RuntimeError('Unknown AwardAutomation version; no files changed.')
    method=text[start:end]
    if NEW in method:
        return 'AwardAutomation compatibility patch already installed.'
    if method.count(OLD)!=1:
        raise RuntimeError('Hardcore streak query differs; no files changed. Review it before installation.')
    backup=target.with_suffix('.py.pre-hardcore-2.1')
    if not backup.exists():backup.write_bytes(target.read_bytes())
    result=text[:start]+method.replace(OLD,NEW)+text[end:]
    compile(result,str(target),'exec')
    target.write_text(result,encoding='utf-8')
    return 'Patched AwardAutomation: support-only sessions do not count toward flight-session medals.'

if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root',default='.')
    print(patch(parser.parse_args().root))
