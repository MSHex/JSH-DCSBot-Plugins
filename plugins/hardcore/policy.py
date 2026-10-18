"""Pure policy shared by live accounting and regression tests."""
from dataclasses import dataclass

SUPPORT_ROLES = frozenset({'instructor', 'artillery_commander', 'forward_observer', 'game-master', 'gamemaster'})


def role_kind(unit_type, slot, side, support_roles=SUPPORT_ROLES):
    if str(unit_type or '').lower() in support_roles:
        return 'support'
    if side in (1, 2) and slot not in (None, -1, '-1', '') and str(unit_type or '').lower() not in {'', '?', 'observer', 'spectator'}:
        return 'aircraft'
    return 'spectator'


def loss_decision(kind, protected, active, ejections_used):
    """Returns (count_loss, revoke, new_ejections_used). Unknown is audit only."""
    if protected is None or protected or kind not in {'ejection', 'death', 'reslot'}:
        return False, False, ejections_used
    if not active:
        return True, False, ejections_used
    if kind == 'ejection':
        return True, ejections_used >= 1, ejections_used + 1
    return True, True, ejections_used


@dataclass(frozen=True)
class Earnings:
    aircraft_blocks: int
    support_blocks: int
    aircraft_half: int
    support_half: int
    net_half: int


def earnings(aircraft_seconds, support_seconds, block_minutes, normal_reward,
             hardcore_reward, hardcore, penalty):
    # Independent category clocks: no partial support block can obtain a flight bonus.
    block = int(block_minutes) * 60
    if block <= 0 or not 0 <= penalty <= 100:
        raise ValueError('Invalid block duration or penalty')
    normal = float(normal_reward) * 2
    premium = float(hardcore_reward) * 2
    if min(normal, premium) < 0 or not normal.is_integer() or not premium.is_integer():
        raise ValueError('Rewards must be nonnegative half-credit increments')
    a, s = max(0, int(aircraft_seconds)) // block, max(0, int(support_seconds)) // block
    ah = a * int(premium if hardcore else normal)
    sh = s * int(normal)
    # Flight losses affect aircraft earnings only; support contributions are separate.
    return Earnings(a, s, ah, sh, ah * (100 - penalty) // 100 + sh)
