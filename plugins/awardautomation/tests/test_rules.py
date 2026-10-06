import unittest

from plugins.awardautomation.rules import (
    CycleAction,
    CycleState,
    QualificationSnapshot,
    air_force_cross_qualified,
    combat_readiness_action,
    completed_milestones,
    hardcore_milestone_action,
    hardcore_session_streak,
    kill_death_ratio,
    one_time_milestone_action,
    recurring_milestone_plan,
    scoped_rule_state_key,
)


class CombatReadinessRuleTest(unittest.TestCase):

    def test_collecting_awards_only_when_both_categories_are_valid(self):
        self.assertEqual(
            CycleAction.NONE,
            combat_readiness_action(
                CycleState.COLLECTING, QualificationSnapshot(True, False)
            ),
        )
        self.assertEqual(
            CycleAction.AWARD,
            combat_readiness_action(
                CycleState.COLLECTING, QualificationSnapshot(True, True)
            ),
        )

    def test_locked_cycle_does_not_reset_on_partial_expiry(self):
        self.assertEqual(
            CycleAction.NONE,
            combat_readiness_action(
                CycleState.AWARDED_LOCKED, QualificationSnapshot(False, True)
            ),
        )
        self.assertEqual(
            CycleAction.NONE,
            combat_readiness_action(
                CycleState.AWARDED_LOCKED, QualificationSnapshot(True, False)
            ),
        )

    def test_locked_cycle_resets_only_when_both_categories_are_invalid(self):
        self.assertEqual(
            CycleAction.RESET,
            combat_readiness_action(
                CycleState.AWARDED_LOCKED, QualificationSnapshot(False, False)
            ),
        )

    def test_existing_holder_is_baselined_by_default(self):
        snapshot = QualificationSnapshot(True, True)
        self.assertEqual(
            CycleAction.BASELINE_LOCK,
            combat_readiness_action(
                CycleState.COLLECTING, snapshot, newly_registered=True
            ),
        )
        self.assertEqual(
            CycleAction.AWARD,
            combat_readiness_action(
                CycleState.COLLECTING,
                snapshot,
                newly_registered=True,
                bootstrap_existing=True,
            ),
        )

    def test_hardcore_streak_counts_successful_billable_sessions(self):
        rows = [
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 75},
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 150},
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 0},
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 75},
        ]
        self.assertEqual(3, hardcore_session_streak(rows))

    def test_revoked_hardcore_session_breaks_streak_even_with_zero_blocks(self):
        rows = [
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 75},
            {'hardcore_at_start': True, 'hardcore_revoked': True, 'gross_half_units': 0},
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 75},
        ]
        self.assertEqual(1, hardcore_session_streak(rows))

    def test_billable_normal_session_breaks_hardcore_streak(self):
        rows = [
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 75},
            {'hardcore_at_start': False, 'hardcore_revoked': False, 'gross_half_units': 50},
            {'hardcore_at_start': True, 'hardcore_revoked': False, 'gross_half_units': 75},
        ]
        self.assertEqual(1, hardcore_session_streak(rows))

    def test_existing_hardcore_milestone_is_baselined_until_streak_breaks(self):
        self.assertEqual(
            CycleAction.BASELINE_LOCK,
            hardcore_milestone_action(
                CycleState.COLLECTING,
                streak=12,
                required_sessions=10,
                newly_registered=True,
            ),
        )
        self.assertEqual(
            CycleAction.NONE,
            hardcore_milestone_action(
                CycleState.AWARDED_LOCKED,
                streak=12,
                required_sessions=10,
            ),
        )
        self.assertEqual(
            CycleAction.RESET,
            hardcore_milestone_action(
                CycleState.AWARDED_LOCKED,
                streak=0,
                required_sessions=10,
            ),
        )

    def test_awarded_hardcore_milestone_never_resets(self):
        self.assertEqual(
            CycleAction.NONE,
            hardcore_milestone_action(
                CycleState.AWARDED_LOCKED,
                streak=0,
                required_sessions=10,
                previously_awarded=True,
            ),
        )

    def test_one_time_statistical_award_is_permanently_locked(self):
        self.assertEqual(
            CycleAction.AWARD,
            one_time_milestone_action(CycleState.COLLECTING, qualified=True),
        )
        self.assertEqual(
            CycleAction.BASELINE_LOCK,
            one_time_milestone_action(
                CycleState.COLLECTING,
                qualified=True,
                newly_registered=True,
            ),
        )
        self.assertEqual(
            CycleAction.NONE,
            one_time_milestone_action(CycleState.AWARDED_LOCKED, qualified=True),
        )

    def test_completed_recurring_milestones_use_whole_thresholds(self):
        self.assertEqual(0, completed_milestones(539999, 540000))
        self.assertEqual(1, completed_milestones(540000, 540000))
        self.assertEqual(2, completed_milestones(1080123, 540000))

    def test_state_keys_separate_campaign_and_server_progress(self):
        self.assertEqual(
            "combat_action:campaign:1",
            scoped_rule_state_key("combat_action", 1),
        )
        self.assertEqual(
            "combat_action:campaign:1:server:Jokers Server",
            scoped_rule_state_key("combat_action", 1, "Jokers Server"),
        )

    def test_recurring_rule_baselines_existing_progress(self):
        self.assertEqual(
            (3, ()),
            recurring_milestone_plan(
                completed=2,
                next_cycle=1,
                newly_registered=True,
            ),
        )

    def test_recurring_rule_awards_every_crossed_cycle(self):
        self.assertEqual(
            (None, (2, 3, 4)),
            recurring_milestone_plan(completed=4, next_cycle=2),
        )
        self.assertEqual(
            (None, (1, 2)),
            recurring_milestone_plan(
                completed=2,
                next_cycle=1,
                newly_registered=True,
                bootstrap_existing=True,
            ),
        )

    def test_kd_ratio_matches_dcsserverbot_zero_death_convention(self):
        self.assertEqual(25.0, kill_death_ratio(50, 2))
        self.assertEqual(51.0, kill_death_ratio(51, 0))

    def test_air_force_cross_requires_hours_and_strictly_over_50_kd(self):
        self.assertFalse(air_force_cross_qualified(
            flight_seconds=1000 * 3600,
            kills=50,
            deaths=1,
            minimum_flight_hours=1000,
            kd_ratio_over=50,
        ))
        self.assertFalse(air_force_cross_qualified(
            flight_seconds=999 * 3600,
            kills=51,
            deaths=1,
            minimum_flight_hours=1000,
            kd_ratio_over=50,
        ))
        self.assertTrue(air_force_cross_qualified(
            flight_seconds=1000 * 3600,
            kills=51,
            deaths=1,
            minimum_flight_hours=1000,
            kd_ratio_over=50,
        ))


if __name__ == '__main__':
    unittest.main()
