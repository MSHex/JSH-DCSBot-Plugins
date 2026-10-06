import asyncio
from dataclasses import dataclass
from typing import Any, cast

import discord
from core import Group, Plugin, PluginRequiredError, get_translation, utils
from discord import app_commands
from discord.ext import tasks
from psycopg.rows import dict_row
from services.bot import DCSServerBot

from .rules import (
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

_ = get_translation(__name__.split('.')[1])
RULE_KEY = "combat_readiness"
HARDCORE_RULE_KEYS = ("air_achievement", "air_commendation")
STAT_RULE_KEYS = (
    "air_force_cross",
    "airmans_medal",
    "aerial_achievement",
    "combat_action",
)
RECURRING_STAT_RULE_KEYS = ("airmans_medal", "combat_action")
DEFAULT_AWARD_IDS = {
    RULE_KEY: 26,
    "air_force_cross": 12,
    "airmans_medal": 14,
    "aerial_achievement": 17,
    "air_achievement": 28,
    "air_commendation": 24,
    "combat_action": 18,
}
DEFAULT_AWARD_NAMES = {
    RULE_KEY: "Combat Readiness Medal",
    "air_force_cross": "Air Force Cross",
    "airmans_medal": "Airman's Medal",
    "aerial_achievement": "Aerial Achievement Medal",
    "air_achievement": "Air Achievement Medal",
    "air_commendation": "Air Commendation Medal",
    "combat_action": "Combat Action Medal",
}
DEFAULT_STAT_RULES = {
    "air_force_cross": {
        "enabled": True,
        "award_id": 12,
        "award_name": "Air Force Cross",
        "credits": 4500,
        "minimum_flight_hours": 1000,
        "kd_ratio_over": 50.0,
    },
    "airmans_medal": {
        "enabled": True,
        "award_id": 14,
        "award_name": "Airman's Medal",
        "credits": 2500,
        "hours_per_award": 150,
        "aircraft_types": ["C-130J-30", "Hercules", "CH-47Fbl1", "UH-1H", "Mi-8MT"],
    },
    "aerial_achievement": {
        "enabled": True,
        "award_id": 17,
        "award_name": "Aerial Achievement Medal",
        "credits": 1500,
        "air_kills": 50,
    },
    "combat_action": {
        "enabled": True,
        "award_id": 18,
        "award_name": "Combat Action Medal",
        "credits": 750,
        "hours_per_award": 500,
    },
}


@dataclass(frozen=True)
class ProcessResult:
    action: CycleAction
    ucid: str
    cycle_number: int
    snapshot: QualificationSnapshot
    award_name: str | None = None
    credited_amount: int = 0
    campaign_name: str | None = None
    detail: str | None = None
    rule_key: str = RULE_KEY
    reason: str | None = None


@dataclass(frozen=True)
class CampaignMetrics:
    flight_seconds: int = 0
    cargo_seconds: int = 0
    kills: int = 0
    deaths: int = 0
    air_kills: int = 0

    @property
    def flight_hours(self) -> float:
        return self.flight_seconds / 3600.0

    @property
    def cargo_hours(self) -> float:
        return self.cargo_seconds / 3600.0

    @property
    def kd_ratio(self) -> float:
        return kill_death_ratio(self.kills, self.deaths)


class AwardAutomation(Plugin):
    awardautomation = Group(
        name="awardautomation",
        description=_("Manage automatic pilot awards")
    )

    def __init__(self, bot: DCSServerBot):
        super().__init__(bot)
        self._scan_lock = asyncio.Lock()
        self._hardcore_error_logged = False

    async def cog_load(self) -> None:
        await super().cog_load()
        await self._validate_configuration()
        if self.get_config().get('enabled', True):
            interval = max(1, int(self.get_config().get('scan_interval_minutes', 1)))
            self.reconcile.change_interval(minutes=interval)
            utils.safe_start(self.reconcile)

    async def cog_unload(self) -> None:
        await utils.safe_cancel(self.reconcile)
        await super().cog_unload()

    def combat_config(self) -> dict[str, Any]:
        return self.get_config().get('rules', {}).get(RULE_KEY, {})

    def campaign_id(self) -> int:
        return int(self.get_config().get('campaign_id', 1))

    def hardcore_server_selector(self) -> str:
        config = self.get_config()
        selector = str(config.get('hardcore', {}).get('server') or '').strip()
        if selector:
            return selector

        # Compatibility with <= 1.5 configurations.
        rules = config.get('rules', {})
        for key in (*HARDCORE_RULE_KEYS, RULE_KEY):
            selector = str(rules.get(key, {}).get('server') or '').strip()
            if selector:
                return selector
        return 'DCS.dcs_serverrelease'

    def hardcore_rules(self) -> list[tuple[str, dict[str, Any]]]:
        rules = self.get_config().get('rules', {})
        return [
            (key, rules[key])
            for key in HARDCORE_RULE_KEYS
            if key in rules and rules[key].get('enabled', True)
        ]

    def stat_rules(self) -> list[tuple[str, dict[str, Any]]]:
        rules = self.get_config().get('rules', {})
        configured = []
        for key in STAT_RULE_KEYS:
            rule = dict(DEFAULT_STAT_RULES[key])
            rule.update(rules.get(key, {}))
            # Missing scope preserves the <= 1.5 campaign-wide behavior.
            rule.setdefault('scope', 'campaign')
            if rule.get('enabled', True):
                configured.append((key, rule))
        return configured

    @staticmethod
    def _stat_scope(rule: dict[str, Any]) -> str:
        return str(rule.get('scope', 'campaign')).strip().casefold()

    @staticmethod
    def _scope_cache_key(rule: dict[str, Any]) -> str:
        scope = AwardAutomation._stat_scope(rule)
        return 'campaign' if scope == 'campaign' else f"server:{rule.get('server', '')}"

    def hardcore_available(self) -> bool:
        return bool(self.bot.cogs.get('Hardcore') and self.hardcore_rules())

    def _qualification_ids(self) -> tuple[set[int], set[int]]:
        qualifications = self.combat_config().get('qualifications', {})
        return (
            {int(value) for value in qualifications.get('aar_ids', [])},
            {int(value) for value in qualifications.get('ifr_ids', [])},
        )

    async def _award_for_rule(
        self,
        cursor,
        rule_key: str,
        rule: dict[str, Any],
        *,
        lock: bool = False,
    ):
        """Resolve an award by stable ID, with a name-only compatibility fallback."""
        suffix = " FOR SHARE" if lock else ""
        award_id = rule.get('award_id', DEFAULT_AWARD_IDS.get(rule_key))
        if award_id is not None:
            await cursor.execute(
                f"SELECT id, name FROM logbook_awards WHERE id = %s{suffix}",
                (int(award_id),),
            )
            return await cursor.fetchone()

        # Compatibility with AwardAutomation <= 1.3 external configurations.
        award_name = str(rule.get('award') or '').strip()
        if not award_name:
            return None
        await cursor.execute(
            f"SELECT id, name FROM logbook_awards WHERE LOWER(name) = LOWER(%s){suffix}",
            (award_name,),
        )
        return await cursor.fetchone()

    @staticmethod
    def _award_label(rule_key: str, rule: dict[str, Any]) -> str:
        return str(
            rule.get('award_name')
            or DEFAULT_AWARD_NAMES.get(rule_key)
            or rule.get('award')
            or f"Award ID {rule.get('award_id', '?')}"
        )

    async def _resolve_server_name(self, cursor, selector: str | None) -> tuple[str, str] | None:
        """Resolve an exact registered server name or exact DCS instance name."""
        if not selector:
            return None

        await cursor.execute(
            "SELECT server_name FROM servers WHERE server_name = %s",
            (selector,),
        )
        server = await cursor.fetchone()
        if server:
            return server['server_name'], 'server'

        await cursor.execute("""
            SELECT DISTINCT server_name
            FROM instances
            WHERE instance = %s AND server_name IS NOT NULL
            ORDER BY server_name
        """, (selector,))
        servers = await cursor.fetchall()
        if len(servers) > 1:
            names = ', '.join(row['server_name'] for row in servers)
            raise RuntimeError(
                f"Instance selector '{selector}' maps to multiple servers ({names}); "
                "configure an exact server name instead."
            )
        if servers:
            return servers[0]['server_name'], 'instance'
        return None

    async def _validate_configuration(self) -> None:
        config = self.get_config()
        if not config.get('enabled', True):
            self.log.info("AwardAutomation is disabled by configuration.")
            return

        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                await cursor.execute(
                    "SELECT id, name, start, stop FROM campaigns WHERE id = %s",
                    (self.campaign_id(),),
                )
                campaign = await cursor.fetchone()
                if not campaign:
                    self.log.error(
                        "AwardAutomation campaign_id %s does not exist.", self.campaign_id()
                    )
                elif not await self._active_campaign(cursor):
                    self.log.warning(
                        "AwardAutomation campaign ID %s ('%s') is not active; awards will be "
                        "held until it becomes active.",
                        campaign['id'], campaign['name'],
                    )

                rules_to_validate = []
                combat_rule = self.combat_config()
                if combat_rule.get('enabled', True):
                    rules_to_validate.append((RULE_KEY, combat_rule))
                rules_to_validate.extend(self.hardcore_rules())
                rules_to_validate.extend(self.stat_rules())

                configured_rules = config.get('rules', {})
                missing_stat_rules = [
                    key for key in STAT_RULE_KEYS if key not in configured_rules
                ]
                if missing_stat_rules:
                    self.log.warning(
                        "AwardAutomation external configuration is missing statistical rules %s; "
                        "built-in campaign-scoped defaults will be used.",
                        ', '.join(missing_stat_rules),
                    )

                for rule_key, rule in rules_to_validate:
                    award = await self._award_for_rule(cursor, rule_key, rule)
                    if not award:
                        award_id = rule.get('award_id', DEFAULT_AWARD_IDS.get(rule_key))
                        if award_id is not None:
                            self.log.error(
                                "AwardAutomation rule '%s' references missing Logbook award ID %s.",
                                rule_key, award_id
                            )
                        else:
                            self.log.error(
                                "AwardAutomation rule '%s' references missing Logbook award '%s'.",
                                rule_key, rule.get('award')
                            )
                    else:
                        expected_name = str(
                            rule.get('award_name')
                            or DEFAULT_AWARD_NAMES.get(rule_key)
                            or ''
                        ).strip()
                        if expected_name and award['name'].casefold() != expected_name.casefold():
                            self.log.warning(
                                "AwardAutomation rule '%s' uses award ID %s, which Logbook names "
                                "'%s' (configured label: '%s'). The ID is authoritative.",
                                rule_key, award['id'], award['name'], expected_name
                            )
                        if rule.get('award_id') is None:
                            self.log.warning(
                                "AwardAutomation rule '%s' has no award_id; using built-in ID %s. "
                                "Add that award_id to the external configuration.",
                                rule_key, award['id']
                            )

                    if rule_key in STAT_RULE_KEYS:
                        scope = self._stat_scope(rule)
                        if scope not in {'campaign', 'server'}:
                            self.log.error(
                                "AwardAutomation rule '%s' has invalid scope '%s'; use "
                                "'campaign' or 'server'.",
                                rule_key, scope,
                            )
                        elif scope == 'server':
                            selector = rule.get('server')
                            if not selector:
                                self.log.error(
                                    "AwardAutomation server-scoped rule '%s' requires server.",
                                    rule_key,
                                )
                            else:
                                try:
                                    resolved_name = await self._resolve_campaign_server(
                                        cursor, selector, rule_key
                                    )
                                    self.log.info(
                                        "AwardAutomation rule '%s' is limited to server '%s'.",
                                        rule_key, resolved_name,
                                    )
                                except RuntimeError as ex:
                                    self.log.error("AwardAutomation rule '%s': %s", rule_key, ex)

                    if rule_key == 'airmans_medal' and not rule.get('aircraft_types'):
                        self.log.error(
                            "AwardAutomation rule 'airmans_medal' requires at least one exact "
                            "statistics.slot value in aircraft_types."
                        )

                if self.hardcore_rules():
                    selector = self.hardcore_server_selector()
                    try:
                        resolved_name = await self._resolve_campaign_server(
                            cursor, selector, 'hardcore'
                        )
                        self.log.info(
                            "AwardAutomation Hardcore medals use server '%s' in campaign ID %s.",
                            resolved_name, self.campaign_id(),
                        )
                    except RuntimeError as ex:
                        self.log.error("AwardAutomation Hardcore configuration: %s", ex)

                if combat_rule.get('enabled', True):
                    aar_ids, ifr_ids = self._qualification_ids()
                    if not aar_ids or not ifr_ids:
                        self.log.error(
                            "Combat Readiness requires non-empty AAR and IFR qualification ID lists."
                        )
                    else:
                        await cursor.execute("SELECT id, name FROM logbook_qualifications ORDER BY id")
                        definitions = {int(row['id']): row['name'] for row in await cursor.fetchall()}
                        missing_aar = aar_ids - definitions.keys()
                        missing_ifr = ifr_ids - definitions.keys()
                        if missing_aar:
                            self.log.warning(
                                "Logbook AAR qualification IDs do not exist: %s.", sorted(missing_aar)
                            )
                        if missing_ifr:
                            self.log.warning(
                                "Logbook IFR qualification IDs do not exist: %s.", sorted(missing_ifr)
                            )

        if self.hardcore_rules() and not self.bot.cogs.get('Hardcore'):
            self.log.info(
                "Hardcore plugin is not loaded; Hardcore milestone awards are inactive. "
                "Combat Readiness remains available."
            )

    async def _candidate_ucids(self) -> list[str]:
        ucids: set[str] = set()
        async with self.apool.connection() as conn:
            async with conn.cursor() as cursor:
                await cursor.execute("""
                    SELECT player_ucid FROM logbook_pilot_qualifications
                    UNION
                    SELECT player_ucid FROM awardautomation_state
                    WHERE POSITION(':campaign:' IN rule_key) = 0
                    UNION
                    SELECT DISTINCT s.player_ucid
                    FROM statistics s
                    JOIN missions m ON m.id = s.mission_id
                    JOIN campaigns_servers cs ON cs.server_name = m.server_name
                    JOIN campaigns c ON c.id = cs.campaign_id
                    WHERE c.id = %s
                      AND (NOW() AT TIME ZONE 'utc')
                          BETWEEN c.start AND COALESCE(c.stop, NOW() AT TIME ZONE 'utc')
                      AND tsrange(
                            s.hop_on,
                            COALESCE(s.hop_off, NOW() AT TIME ZONE 'utc'),
                            '[)'
                          ) && tsrange(
                            c.start,
                            COALESCE(c.stop, NOW() AT TIME ZONE 'utc'),
                            '[)'
                          )
                """, (self.campaign_id(),))
                ucids.update(row[0] for row in await cursor.fetchall())

        if self.hardcore_available():
            try:
                async with self.apool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cursor:
                        server_name = await self._resolve_campaign_server(
                            cursor, self.hardcore_server_selector(), 'hardcore'
                        )
                        await cursor.execute("""
                            SELECT DISTINCT player_ucid
                            FROM hardcore_sessions
                            WHERE settled = TRUE
                              AND campaign_id = %s
                              AND server_name = %s
                        """, (self.campaign_id(), server_name))
                        ucids.update(row['player_ucid'] for row in await cursor.fetchall())
                self._hardcore_error_logged = False
            except Exception:
                if not self._hardcore_error_logged:
                    self.log.exception(
                        "Hardcore milestone scanning is unavailable. Verify SELECT permission "
                        "for hardcore_sessions. Combat Readiness will continue."
                    )
                    self._hardcore_error_logged = True
        return sorted(ucids)

    async def _qualification_snapshot(self, cursor, ucid: str) -> QualificationSnapshot:
        await cursor.execute("""
            SELECT q.id, q.name,
                   (pq.expires_at IS NULL OR
                    pq.expires_at > (NOW() AT TIME ZONE 'utc')) AS is_valid
            FROM logbook_pilot_qualifications pq
            JOIN logbook_qualifications q ON q.id = pq.qualification_id
            WHERE pq.player_ucid = %s
            ORDER BY q.name
        """, (ucid,))
        rows = await cursor.fetchall()
        aar_ids, ifr_ids = self._qualification_ids()
        aar_names = tuple(
            row['name'] for row in rows
            if row['is_valid'] and int(row['id']) in aar_ids
        )
        ifr_names = tuple(
            row['name'] for row in rows
            if row['is_valid'] and int(row['id']) in ifr_ids
        )
        return QualificationSnapshot(
            aar_valid=bool(aar_names),
            ifr_valid=bool(ifr_names),
            aar_names=aar_names,
            ifr_names=ifr_names,
        )

    async def _active_campaign(self, cursor) -> dict | None:
        """Return the configured campaign only while it is active."""
        await cursor.execute("""
            SELECT id, name, start, stop
            FROM campaigns
            WHERE id = %s
              AND (NOW() AT TIME ZONE 'utc')
                  BETWEEN start AND COALESCE(stop, NOW() AT TIME ZONE 'utc')
        """, (self.campaign_id(),))
        return await cursor.fetchone()

    async def _server_in_campaign(self, cursor, server_name: str) -> bool:
        await cursor.execute("""
            SELECT 1
            FROM campaigns_servers
            WHERE campaign_id = %s AND server_name = %s
        """, (self.campaign_id(), server_name))
        return bool(await cursor.fetchone())

    async def _resolve_campaign_server(
        self,
        cursor,
        selector: str | None,
        rule_key: str,
    ) -> str:
        if not selector:
            raise RuntimeError(
                f"AwardAutomation rule '{rule_key}' requires a server selector."
            )
        resolved = await self._resolve_server_name(cursor, selector)
        if not resolved:
            raise RuntimeError(
                f"Server selector '{selector}' matches neither a registered server "
                "name nor a DCS instance name."
            )
        server_name, _ = resolved
        if not await self._server_in_campaign(cursor, server_name):
            raise RuntimeError(
                f"Server '{server_name}' is not assigned to campaign ID {self.campaign_id()}."
            )
        return server_name

    async def _stat_scope_server(
        self,
        cursor,
        rule_key: str,
        rule: dict[str, Any],
    ) -> str | None:
        scope = self._stat_scope(rule)
        if scope == 'campaign':
            return None
        if scope != 'server':
            raise RuntimeError(
                f"AwardAutomation rule '{rule_key}' has unsupported scope '{scope}'."
            )
        return await self._resolve_campaign_server(cursor, rule.get('server'), rule_key)

    def _credit_cap(self) -> int | None:
        try:
            value = self.get_config(plugin_name='creditsystem').get('max_points')
            return int(value) if value is not None else None
        except (TypeError, ValueError):
            self.log.warning("CreditSystem max_points is invalid; no cap will be applied.")
            return None

    async def _grant_award(
        self,
        cursor,
        ucid: str,
        rule_key: str,
        cycle_number: int,
        snapshot: QualificationSnapshot,
        rule: dict[str, Any],
        citation: str,
        reason: str,
        *,
        campaign: dict | None = None,
        state_after: CycleState = CycleState.AWARDED_LOCKED,
        cycle_after: int | None = None,
        state_rule_key: str | None = None,
    ) -> ProcessResult:
        state_key = state_rule_key or rule_key
        reward = max(0, int(rule.get('credits', 0)))

        award = await self._award_for_rule(cursor, rule_key, rule, lock=True)
        if not award:
            award_id = rule.get('award_id', DEFAULT_AWARD_IDS.get(rule_key))
            reference = (
                f"ID {award_id}"
                if award_id is not None
                else repr(rule.get('award'))
            )
            return ProcessResult(
                CycleAction.NONE, ucid, cycle_number, snapshot,
                detail=f"Logbook award {reference} was not found.",
                rule_key=rule_key,
            )

        if campaign is None:
            campaign = await self._active_campaign(cursor)
        if not campaign:
            return ProcessResult(
                CycleAction.NONE, ucid, cycle_number, snapshot,
                detail=(
                    f"Campaign ID {self.campaign_id()} does not exist or is not active; "
                    "the medal and credit reward were held."
                ),
                rule_key=rule_key,
            )

        await cursor.execute("""
            INSERT INTO awardautomation_ledger
                (player_ucid, rule_key, cycle_number, award_id, campaign_id,
                 configured_credit_reward, credited_amount, old_credits, new_credits)
            VALUES (%s, %s, %s, %s, %s, %s, 0, 0, 0)
            ON CONFLICT (player_ucid, rule_key, cycle_number) DO NOTHING
            RETURNING id
        """, (
            ucid, state_key, cycle_number, award['id'], campaign['id'], reward
        ))
        ledger = await cursor.fetchone()
        if not ledger:
            await cursor.execute("""
                UPDATE awardautomation_state
                SET state = %s,
                    cycle_number = %s,
                    updated_at = NOW() AT TIME ZONE 'utc'
                WHERE player_ucid = %s AND rule_key = %s
            """, (
                state_after.value,
                cycle_after if cycle_after is not None else cycle_number,
                ucid,
                state_key,
            ))
            return ProcessResult(
                CycleAction.NONE, ucid, cycle_number, snapshot,
                detail="This cycle already exists in the automation ledger.",
                rule_key=rule_key,
            )

        await cursor.execute("""
            INSERT INTO credits (campaign_id, player_ucid, points)
            VALUES (%s, %s, 0)
            ON CONFLICT (campaign_id, player_ucid) DO NOTHING
        """, (campaign['id'], ucid))
        await cursor.execute("""
            SELECT points FROM credits
            WHERE campaign_id = %s AND player_ucid = %s
            FOR UPDATE
        """, (campaign['id'], ucid))
        old_credits = int((await cursor.fetchone())['points'])
        cap = self._credit_cap()
        new_credits = old_credits + reward
        if cap is not None:
            new_credits = min(new_credits, cap)
        credited_amount = max(0, new_credits - old_credits)

        await cursor.execute("""
            UPDATE credits SET points = %s
            WHERE campaign_id = %s AND player_ucid = %s
        """, (new_credits, campaign['id'], ucid))
        await cursor.execute("""
            INSERT INTO credits_log
                (campaign_id, event, player_ucid, old_points, new_points, remark)
            VALUES (%s, 'award', %s, %s, %s, %s)
        """, (
            campaign['id'], ucid, old_credits, new_credits,
            f"Automatic reward: {award['name']} ({rule_key}, cycle {cycle_number})"
        ))
        await cursor.execute("""
            INSERT INTO logbook_pilot_awards
                (player_ucid, award_id, granted_by, granted_at, citation)
            VALUES (
                %s,
                %s,
                NULL,
                (NOW() AT TIME ZONE 'utc') +
                    (MOD(%s, 1000000) * INTERVAL '1 microsecond'),
                %s
            )
        """, (
            ucid, award['id'], ledger['id'], citation
        ))
        await cursor.execute("""
            UPDATE awardautomation_ledger
            SET credited_amount = %s, old_credits = %s, new_credits = %s,
                granted_at = NOW() AT TIME ZONE 'utc'
            WHERE id = %s
        """, (credited_amount, old_credits, new_credits, ledger['id']))
        await cursor.execute("""
            UPDATE awardautomation_state
            SET state = %s,
                cycle_number = %s,
                last_awarded_at = NOW() AT TIME ZONE 'utc',
                updated_at = NOW() AT TIME ZONE 'utc'
            WHERE player_ucid = %s AND rule_key = %s
        """, (
            state_after.value,
            cycle_after if cycle_after is not None else cycle_number,
            ucid,
            state_key,
        ))

        return ProcessResult(
            CycleAction.AWARD,
            ucid,
            cycle_number,
            snapshot,
            award_name=award['name'],
            credited_amount=credited_amount,
            campaign_name=campaign['name'],
            rule_key=rule_key,
            reason=reason,
        )

    async def _campaign_metrics(
        self,
        cursor,
        ucid: str,
        rule_key: str,
        rule: dict[str, Any],
        cargo_aircraft: list[str],
    ) -> tuple[dict | None, CampaignMetrics | None, str | None]:
        campaign = await self._active_campaign(cursor)
        if not campaign:
            return None, None, None

        scope_server = await self._stat_scope_server(cursor, rule_key, rule)

        normalized_aircraft = sorted({
            str(value).strip().casefold()
            for value in cargo_aircraft
            if str(value).strip()
        })
        await cursor.execute("""
            SELECT
                COALESCE(ROUND(SUM(GREATEST(0, EXTRACT(EPOCH FROM (
                    LEAST(
                        COALESCE(s.hop_off, NOW() AT TIME ZONE 'utc'),
                        COALESCE(c.stop, NOW() AT TIME ZONE 'utc')
                    ) - GREATEST(s.hop_on, c.start)
                ))))), 0)::BIGINT AS flight_seconds,
                COALESCE(ROUND(SUM(CASE
                    WHEN LOWER(s.slot) = ANY(%s::TEXT[]) THEN
                        GREATEST(0, EXTRACT(EPOCH FROM (
                            LEAST(
                                COALESCE(s.hop_off, NOW() AT TIME ZONE 'utc'),
                                COALESCE(c.stop, NOW() AT TIME ZONE 'utc')
                            ) - GREATEST(s.hop_on, c.start)
                        )))
                    ELSE 0
                END)), 0)::BIGINT AS cargo_seconds,
                COALESCE(SUM(s.kills), 0)::BIGINT AS kills,
                COALESCE(SUM(
                    COALESCE(s.deaths_planes, 0) +
                    COALESCE(s.deaths_helicopters, 0) +
                    COALESCE(s.deaths_ships, 0) +
                    COALESCE(s.deaths_sams, 0) +
                    COALESCE(s.deaths_ground, 0)
                ), 0)::BIGINT AS deaths,
                COALESCE(SUM(
                    COALESCE(s.kills_planes, 0) +
                    COALESCE(s.kills_helicopters, 0)
                ), 0)::BIGINT AS air_kills
            FROM statistics s
            JOIN missions m ON m.id = s.mission_id
            JOIN campaigns c ON c.id = %s
            JOIN campaigns_servers cs
              ON cs.campaign_id = c.id AND cs.server_name = m.server_name
            WHERE s.player_ucid = %s
              AND (%s::TEXT IS NULL OR m.server_name = %s)
              AND tsrange(
                    s.hop_on,
                    COALESCE(s.hop_off, NOW() AT TIME ZONE 'utc'),
                    '[)'
                  ) && tsrange(
                    c.start,
                    COALESCE(c.stop, NOW() AT TIME ZONE 'utc'),
                    '[)'
                  )
        """, (
            normalized_aircraft,
            campaign['id'],
            ucid,
            scope_server,
            scope_server,
        ))
        row = await cursor.fetchone()
        return campaign, CampaignMetrics(
            flight_seconds=int(row['flight_seconds'] or 0),
            cargo_seconds=int(row['cargo_seconds'] or 0),
            kills=int(row['kills'] or 0),
            deaths=int(row['deaths'] or 0),
            air_kills=int(row['air_kills'] or 0),
        ), scope_server

    async def _state_for_rule(self, cursor, ucid: str, rule_key: str) -> tuple[bool, dict]:
        await cursor.execute("""
            INSERT INTO awardautomation_state
                (player_ucid, rule_key, state, cycle_number)
            VALUES (%s, %s, %s, 1)
            ON CONFLICT (player_ucid, rule_key) DO NOTHING
            RETURNING player_ucid
        """, (ucid, rule_key, CycleState.COLLECTING.value))
        newly_registered = bool(await cursor.fetchone())
        await cursor.execute("""
            SELECT state, cycle_number, last_awarded_at
            FROM awardautomation_state
            WHERE player_ucid = %s AND rule_key = %s
            FOR UPDATE
        """, (ucid, rule_key))
        return newly_registered, await cursor.fetchone()

    async def _process_one_time_stat_rule(
        self,
        cursor,
        ucid: str,
        rule_key: str,
        rule: dict[str, Any],
        campaign: dict,
        scope_server: str | None,
        qualified: bool,
        citation: str,
        reason: str,
    ) -> ProcessResult:
        snapshot = QualificationSnapshot(False, False)
        state_key = scoped_rule_state_key(rule_key, campaign['id'], scope_server)
        newly_registered, state_row = await self._state_for_rule(cursor, ucid, state_key)
        cycle_number = int(state_row['cycle_number'])
        action = one_time_milestone_action(
            CycleState(state_row['state']),
            qualified=qualified,
            newly_registered=newly_registered,
            bootstrap_existing=bool(self.get_config().get('bootstrap_existing', False)),
        )
        if action == CycleAction.BASELINE_LOCK:
            await cursor.execute("""
                UPDATE awardautomation_state
                SET state = %s, updated_at = NOW() AT TIME ZONE 'utc'
                WHERE player_ucid = %s AND rule_key = %s
            """, (CycleState.AWARDED_LOCKED.value, ucid, state_key))
            return ProcessResult(
                action, ucid, cycle_number, snapshot, rule_key=rule_key,
                reason="the requirement was already met at the first scan.",
            )
        if action == CycleAction.AWARD:
            return await self._grant_award(
                cursor,
                ucid,
                rule_key,
                cycle_number,
                snapshot,
                rule,
                citation,
                reason,
                campaign=campaign,
                state_rule_key=state_key,
            )
        return ProcessResult(action, ucid, cycle_number, snapshot, rule_key=rule_key)

    async def _process_recurring_stat_rule(
        self,
        cursor,
        ucid: str,
        rule_key: str,
        rule: dict[str, Any],
        campaign: dict,
        scope_server: str | None,
        completed: int,
        milestone_hours: int,
        metric_name: str,
    ) -> list[ProcessResult]:
        snapshot = QualificationSnapshot(False, False)
        state_key = scoped_rule_state_key(rule_key, campaign['id'], scope_server)
        newly_registered, state_row = await self._state_for_rule(cursor, ucid, state_key)
        next_cycle = int(state_row['cycle_number'])
        baseline_cycle, award_cycles = recurring_milestone_plan(
            completed,
            next_cycle,
            newly_registered=newly_registered,
            bootstrap_existing=bool(self.get_config().get('bootstrap_existing', False)),
        )
        if baseline_cycle is not None:
            await cursor.execute("""
                UPDATE awardautomation_state
                SET state = %s, cycle_number = %s,
                    updated_at = NOW() AT TIME ZONE 'utc'
                WHERE player_ucid = %s AND rule_key = %s
            """, (
                CycleState.COLLECTING.value,
                baseline_cycle,
                ucid,
                state_key,
            ))
            return [ProcessResult(
                CycleAction.BASELINE_LOCK,
                ucid,
                baseline_cycle,
                snapshot,
                rule_key=rule_key,
                reason=f"existing progress was baselined through milestone {completed}.",
            )]

        if not award_cycles:
            return [ProcessResult(
                CycleAction.NONE,
                ucid,
                next_cycle,
                snapshot,
                rule_key=rule_key,
            )]

        results: list[ProcessResult] = []
        scope_label = f"server {scope_server}" if scope_server else "the full campaign"
        for cycle_number in award_cycles:
            threshold_hours = cycle_number * milestone_hours
            result = await self._grant_award(
                cursor,
                ucid,
                rule_key,
                cycle_number,
                snapshot,
                rule,
                (
                    f"Automatically awarded for reaching {threshold_hours:,} {metric_name} "
                    f"hours across {scope_label} (milestone {cycle_number})."
                ),
                f"reached {threshold_hours:,} {metric_name} hours across {scope_label}.",
                campaign=campaign,
                state_after=CycleState.COLLECTING,
                cycle_after=cycle_number + 1,
                state_rule_key=state_key,
            )
            results.append(result)
        return results

    async def process_stat_milestones(self, ucid: str) -> list[ProcessResult]:
        rules = self.stat_rules()
        if not rules:
            return []

        results: list[ProcessResult] = []
        empty_snapshot = QualificationSnapshot(False, False)
        cargo_by_scope = {
            self._scope_cache_key(rule): list(rule.get('aircraft_types', []))
            for rule_key, rule in rules
            if rule_key == 'airmans_medal'
        }

        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                metrics_cache: dict[
                    str,
                    tuple[dict | None, CampaignMetrics | None, str | None],
                ] = {}
                for rule_key, rule in rules:
                    scope_key = self._scope_cache_key(rule)
                    if scope_key not in metrics_cache:
                        metrics_cache[scope_key] = await self._campaign_metrics(
                            cursor,
                            ucid,
                            rule_key,
                            rule,
                            cargo_by_scope.get(scope_key, []),
                        )
                    campaign, metrics, scope_server = metrics_cache[scope_key]
                    if not campaign or not metrics:
                        results.append(ProcessResult(
                            CycleAction.NONE,
                            ucid,
                            1,
                            empty_snapshot,
                            detail=(
                                f"Campaign ID {self.campaign_id()} does not exist or is not active "
                                "for statistical awards."
                            ),
                            rule_key=rule_key,
                        ))
                        continue

                    if rule_key == 'air_force_cross':
                        minimum_hours = int(rule.get('minimum_flight_hours', 1000))
                        kd_ratio_over = float(rule.get('kd_ratio_over', 50))
                        scope_label = (
                            f"server {scope_server}" if scope_server else "the full campaign"
                        )
                        results.append(await self._process_one_time_stat_rule(
                            cursor,
                            ucid,
                            rule_key,
                            rule,
                            campaign,
                            scope_server,
                            air_force_cross_qualified(
                                flight_seconds=metrics.flight_seconds,
                                kills=metrics.kills,
                                deaths=metrics.deaths,
                                minimum_flight_hours=minimum_hours,
                                kd_ratio_over=kd_ratio_over,
                            ),
                            (
                                "Automatically awarded for exceeding a "
                                f"{kd_ratio_over:g} K/D ratio with at least "
                                f"{minimum_hours:,} flight hours across {scope_label}."
                            ),
                            (
                                f"exceeded a {kd_ratio_over:g} K/D ratio after "
                                f"{minimum_hours:,} flight hours across {scope_label}."
                            ),
                        ))
                    elif rule_key == 'aerial_achievement':
                        required_kills = int(rule.get('air_kills', 50))
                        scope_label = (
                            f"server {scope_server}" if scope_server else "the full campaign"
                        )
                        results.append(await self._process_one_time_stat_rule(
                            cursor,
                            ucid,
                            rule_key,
                            rule,
                            campaign,
                            scope_server,
                            metrics.air_kills >= required_kills,
                            (
                                f"Automatically awarded for reaching {required_kills:,} "
                                f"air-to-air kills across {scope_label}."
                            ),
                            f"reached {required_kills:,} air-to-air kills across {scope_label}.",
                        ))
                    elif rule_key == 'airmans_medal':
                        hours_per_award = int(rule.get('hours_per_award', 150))
                        results.extend(await self._process_recurring_stat_rule(
                            cursor,
                            ucid,
                            rule_key,
                            rule,
                            campaign,
                            scope_server,
                            completed_milestones(
                                metrics.cargo_seconds,
                                hours_per_award * 3600,
                            ),
                            hours_per_award,
                            "cargo-flight",
                        ))
                    elif rule_key == 'combat_action':
                        hours_per_award = int(rule.get('hours_per_award', 500))
                        results.extend(await self._process_recurring_stat_rule(
                            cursor,
                            ucid,
                            rule_key,
                            rule,
                            campaign,
                            scope_server,
                            completed_milestones(
                                metrics.flight_seconds,
                                hours_per_award * 3600,
                            ),
                            hours_per_award,
                            "flight",
                        ))

        return results

    async def process_player(self, ucid: str) -> ProcessResult:
        config = self.get_config()
        if not config.get('enabled', True) or not self.combat_config().get('enabled', True):
            return ProcessResult(
                CycleAction.NONE, ucid, 1,
                QualificationSnapshot(False, False),
                detail="Automation or Combat Readiness rule is disabled."
            )

        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                campaign = await self._active_campaign(cursor)
                if not campaign:
                    return ProcessResult(
                        CycleAction.NONE,
                        ucid,
                        1,
                        QualificationSnapshot(False, False),
                        detail=(
                            f"Campaign ID {self.campaign_id()} does not exist or is not active; "
                            "Combat Readiness processing is paused."
                        ),
                    )
                snapshot = await self._qualification_snapshot(cursor, ucid)
                await cursor.execute("""
                    INSERT INTO awardautomation_state
                        (player_ucid, rule_key, state, cycle_number)
                    VALUES (%s, %s, %s, 1)
                    ON CONFLICT (player_ucid, rule_key) DO NOTHING
                    RETURNING player_ucid
                """, (ucid, RULE_KEY, CycleState.COLLECTING.value))
                newly_registered = bool(await cursor.fetchone())

                await cursor.execute("""
                    SELECT state, cycle_number
                    FROM awardautomation_state
                    WHERE player_ucid = %s AND rule_key = %s
                    FOR UPDATE
                """, (ucid, RULE_KEY))
                state_row = await cursor.fetchone()
                state = CycleState(state_row['state'])
                cycle_number = int(state_row['cycle_number'])
                action = combat_readiness_action(
                    state,
                    snapshot,
                    newly_registered=newly_registered,
                    bootstrap_existing=bool(config.get('bootstrap_existing', False)),
                )

                if action == CycleAction.BASELINE_LOCK:
                    await cursor.execute("""
                        UPDATE awardautomation_state
                        SET state = %s, updated_at = NOW() AT TIME ZONE 'utc'
                        WHERE player_ucid = %s AND rule_key = %s
                    """, (CycleState.AWARDED_LOCKED.value, ucid, RULE_KEY))
                    return ProcessResult(action, ucid, cycle_number, snapshot)

                if action == CycleAction.RESET:
                    cycle_number += 1
                    await cursor.execute("""
                        UPDATE awardautomation_state
                        SET state = %s, cycle_number = %s,
                            last_reset_at = NOW() AT TIME ZONE 'utc',
                            updated_at = NOW() AT TIME ZONE 'utc'
                        WHERE player_ucid = %s AND rule_key = %s
                    """, (
                        CycleState.COLLECTING.value, cycle_number, ucid, RULE_KEY
                    ))
                    return ProcessResult(action, ucid, cycle_number, snapshot)

                if action == CycleAction.AWARD:
                    return await self._grant_award(
                        cursor,
                        ucid,
                        RULE_KEY,
                        cycle_number,
                        snapshot,
                        self.combat_config(),
                        (
                            "Automatically awarded after earning valid AAR and IFR qualifications "
                            f"(Combat Readiness cycle {cycle_number})."
                        ),
                        "now holds valid AAR and IFR qualifications.",
                        campaign=campaign,
                    )

                return ProcessResult(action, ucid, cycle_number, snapshot)

    async def _hardcore_streak(
        self,
        cursor,
        ucid: str,
        campaign_id: int,
        server_name: str,
    ) -> int:
        await cursor.execute("""
            SELECT hardcore_at_start, hardcore_revoked, gross_half_units
            FROM hardcore_sessions
            WHERE player_ucid = %s
              AND settled = TRUE
              AND campaign_id = %s
              AND server_name = %s
            ORDER BY COALESCE(settled_at, connected_at) DESC, id DESC
        """, (ucid, campaign_id, server_name))
        return hardcore_session_streak(await cursor.fetchall())

    async def process_hardcore_milestones(self, ucid: str) -> list[ProcessResult]:
        if not self.hardcore_available():
            return []

        results: list[ProcessResult] = []
        empty_snapshot = QualificationSnapshot(False, False)
        bootstrap_existing = bool(self.get_config().get('bootstrap_existing', False))

        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                campaign = await self._active_campaign(cursor)
                if not campaign:
                    return [ProcessResult(
                        CycleAction.NONE,
                        ucid,
                        1,
                        empty_snapshot,
                        detail=(
                            f"Campaign ID {self.campaign_id()} does not exist or is not active "
                            "for Hardcore awards."
                        ),
                        rule_key=rule_key,
                    ) for rule_key, _ in self.hardcore_rules()]

                server_name = await self._resolve_campaign_server(
                    cursor, self.hardcore_server_selector(), 'hardcore'
                )
                streak = await self._hardcore_streak(
                    cursor, ucid, campaign['id'], server_name
                )

                for rule_key, rule in self.hardcore_rules():
                    required_sessions = max(1, int(rule.get('sessions', 1)))
                    await cursor.execute("""
                        INSERT INTO awardautomation_state
                            (player_ucid, rule_key, state, cycle_number)
                        VALUES (%s, %s, %s, 1)
                        ON CONFLICT (player_ucid, rule_key) DO NOTHING
                        RETURNING player_ucid
                    """, (ucid, rule_key, CycleState.COLLECTING.value))
                    newly_registered = bool(await cursor.fetchone())

                    await cursor.execute("""
                        SELECT state, cycle_number, last_awarded_at
                        FROM awardautomation_state
                        WHERE player_ucid = %s AND rule_key = %s
                        FOR UPDATE
                    """, (ucid, rule_key))
                    state_row = await cursor.fetchone()
                    cycle_number = int(state_row['cycle_number'])
                    action = hardcore_milestone_action(
                        CycleState(state_row['state']),
                        streak=streak,
                        required_sessions=required_sessions,
                        newly_registered=newly_registered,
                        bootstrap_existing=bootstrap_existing,
                        previously_awarded=state_row['last_awarded_at'] is not None,
                    )
                    if action == CycleAction.NONE:
                        continue

                    if action == CycleAction.RESET:
                        cycle_number += 1
                        await cursor.execute("""
                            UPDATE awardautomation_state
                            SET state = %s, cycle_number = %s,
                                last_reset_at = NOW() AT TIME ZONE 'utc',
                                updated_at = NOW() AT TIME ZONE 'utc'
                            WHERE player_ucid = %s AND rule_key = %s
                        """, (
                            CycleState.COLLECTING.value, cycle_number, ucid, rule_key
                        ))
                        results.append(ProcessResult(
                            CycleAction.RESET,
                            ucid,
                            cycle_number,
                            empty_snapshot,
                            rule_key=rule_key,
                            reason="the inherited Hardcore streak ended; a new streak can now qualify.",
                        ))
                        continue

                    if action == CycleAction.BASELINE_LOCK:
                        await cursor.execute("""
                            UPDATE awardautomation_state
                            SET state = %s, updated_at = NOW() AT TIME ZONE 'utc'
                            WHERE player_ucid = %s AND rule_key = %s
                        """, (CycleState.AWARDED_LOCKED.value, ucid, rule_key))
                        results.append(ProcessResult(
                            CycleAction.BASELINE_LOCK,
                            ucid,
                            cycle_number,
                            empty_snapshot,
                            rule_key=rule_key,
                            reason=f"already had a {streak}-session Hardcore streak at first scan.",
                        ))
                        continue

                    results.append(await self._grant_award(
                        cursor,
                        ucid,
                        rule_key,
                        cycle_number,
                        empty_snapshot,
                        rule,
                        (
                            f"Automatically awarded for maintaining Hardcore status through "
                            f"{required_sessions} consecutive qualifying sessions on "
                            f"{server_name} in campaign {campaign['name']}."
                        ),
                        (
                            f"completed {required_sessions} consecutive qualifying Hardcore sessions "
                            f"on {server_name} without Hardcore being revoked."
                        ),
                        campaign=campaign,
                    ))

        return results

    async def _sync_online_credits(self, ucid: str) -> None:
        for server in self.bot.servers.values():
            player = server.get_player(ucid=ucid)
            if not player or not hasattr(player, 'get_points'):
                continue
            points = await player.get_points()
            await server.send_to_dcs({
                'command': 'updateUserPoints',
                'ucid': ucid,
                'points': points,
            })

    async def _announce(self, result: ProcessResult) -> None:
        if result.action != CycleAction.AWARD:
            return
        await self._sync_online_credits(result.ucid)
        member = await self.bot.get_member_by_ucid(result.ucid)
        pilot_name = member.display_name if member else result.ucid
        message = (
            f"{pilot_name} earned {result.award_name} and received "
            f"{result.credited_amount:,} campaign credits."
        )
        self.log.info(message)
        await self.bot.audit(message, user=member or result.ucid)

        embed = discord.Embed(
            title=_("{} Awarded").format(result.award_name),
            description=f"{pilot_name} {result.reason or _('met the configured award criteria.')}",
            color=discord.Color.gold(),
        )
        embed.add_field(name=_("Credits"), value=f"+{result.credited_amount:,}")
        embed.add_field(name=_("Campaign"), value=result.campaign_name or _('Unknown'))
        embed.set_footer(text=result.rule_key.replace('_', ' ').title())

        config = self.get_config()
        if member and config.get('notify_pilot', True):
            try:
                await member.send(embed=embed)
            except (discord.Forbidden, discord.HTTPException):
                self.log.debug("Could not DM automatic award to %s.", pilot_name)

        channel_id = config.get('notify_channel')
        if channel_id:
            channel = self.bot.get_channel(int(channel_id))
            if channel and hasattr(channel, 'send'):
                await channel.send(embed=embed)
            else:
                self.log.warning("Award notification channel %s was not found.", channel_id)

    async def scan(self, ucid: str | None = None) -> list[ProcessResult]:
        async with self._scan_lock:
            ucids = [ucid] if ucid else await self._candidate_ucids()
            results = []
            for player_ucid in ucids:
                try:
                    result = await self.process_player(player_ucid)
                    results.append(result)
                    await self._announce(result)
                except Exception as ex:
                    self.log.exception(
                        "AwardAutomation failed while processing player %s.", player_ucid
                    )
                    results.append(ProcessResult(
                        CycleAction.NONE,
                        player_ucid,
                        1,
                        QualificationSnapshot(False, False),
                        detail=str(ex),
                    ))
                try:
                    stat_results = await self.process_stat_milestones(player_ucid)
                    results.extend(stat_results)
                    for result in stat_results:
                        await self._announce(result)
                except Exception as ex:
                    self.log.exception(
                        "Statistical award processing failed for player %s.", player_ucid
                    )
                    results.append(ProcessResult(
                        CycleAction.NONE,
                        player_ucid,
                        1,
                        QualificationSnapshot(False, False),
                        detail=f"Statistical awards unavailable: {ex}",
                        rule_key="statistics",
                    ))
                if self.hardcore_available():
                    try:
                        hardcore_results = await self.process_hardcore_milestones(player_ucid)
                        results.extend(hardcore_results)
                        for result in hardcore_results:
                            await self._announce(result)
                        self._hardcore_error_logged = False
                    except Exception as ex:
                        if not self._hardcore_error_logged:
                            self.log.exception(
                                "Hardcore milestone processing failed. Verify SELECT permission for "
                                "hardcore_sessions. Combat Readiness will continue."
                            )
                            self._hardcore_error_logged = True
                        results.append(ProcessResult(
                            CycleAction.NONE,
                            player_ucid,
                            1,
                            QualificationSnapshot(False, False),
                            detail=f"Hardcore milestones unavailable: {ex}",
                            rule_key="hardcore",
                        ))
            return results

    @tasks.loop(minutes=1)
    async def reconcile(self):
        await self.scan()

    @reconcile.before_loop
    async def before_reconcile(self):
        await self.bot.wait_until_ready()

    async def _status_row(self, ucid: str) -> dict:
        async with self.apool.connection() as conn:
            async with conn.cursor(row_factory=dict_row) as cursor:
                snapshot = await self._qualification_snapshot(cursor, ucid)
                await cursor.execute("""
                    SELECT rule_key, state, cycle_number, last_awarded_at, last_reset_at, updated_at
                    FROM awardautomation_state
                    WHERE player_ucid = %s
                """, (ucid,))
                states = {row['rule_key']: dict(row) for row in await cursor.fetchall()}

                stat_progress: dict[str, dict[str, Any]] = {}
                stat_errors: dict[str, str] = {}
                cargo_by_scope = {
                    self._scope_cache_key(rule): list(rule.get('aircraft_types', []))
                    for rule_key, rule in self.stat_rules()
                    if rule_key == 'airmans_medal'
                }
                metrics_cache: dict[
                    str,
                    tuple[dict | None, CampaignMetrics | None, str | None],
                ] = {}
                for rule_key, rule in self.stat_rules():
                    scope_key = self._scope_cache_key(rule)
                    try:
                        if scope_key not in metrics_cache:
                            metrics_cache[scope_key] = await self._campaign_metrics(
                                cursor,
                                ucid,
                                rule_key,
                                rule,
                                cargo_by_scope.get(scope_key, []),
                            )
                        campaign, metrics, scope_server = metrics_cache[scope_key]
                        if campaign and metrics:
                            stat_progress[rule_key] = {
                                'campaign': campaign,
                                'metrics': metrics,
                                'scope_server': scope_server,
                            }
                        else:
                            stat_errors[rule_key] = _("Configured campaign is not active")
                    except Exception as ex:
                        stat_errors[rule_key] = str(ex)

        data: dict[str, Any] = {
            'snapshot': snapshot,
            'states': states,
            'stat_progress': stat_progress,
            'stat_errors': stat_errors,
            'hardcore_streak': None,
            'hardcore_error': None,
        }
        if self.hardcore_available():
            try:
                async with self.apool.connection() as conn:
                    async with conn.cursor(row_factory=dict_row) as cursor:
                        campaign = await self._active_campaign(cursor)
                        if not campaign:
                            raise RuntimeError(
                                f"Campaign ID {self.campaign_id()} is not active."
                            )
                        server_name = await self._resolve_campaign_server(
                            cursor, self.hardcore_server_selector(), 'hardcore'
                        )
                        data['hardcore_streak'] = await self._hardcore_streak(
                            cursor, ucid, campaign['id'], server_name
                        )
                        data['hardcore_server'] = server_name
            except Exception as ex:
                data['hardcore_error'] = str(ex)
        return data

    @awardautomation.command(name="status", description=_("Show automatic-award state for a pilot"))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.describe(user=_("Pilot to inspect"))
    async def automation_status(self, interaction: discord.Interaction, user: discord.Member):
        ucid = await self.bot.get_ucid_by_member(user)
        if not ucid:
            await interaction.response.send_message(
                _("User {} is not linked to DCS.").format(user.display_name), ephemeral=True
            )
            return
        row = await self._status_row(ucid)
        snapshot = cast(QualificationSnapshot, row['snapshot'])
        combat_state = row['states'].get(RULE_KEY)
        embed = discord.Embed(
            title=_("Award Automation Status"),
            description=user.mention,
            color=discord.Color.blue(),
        )
        embed.add_field(
            name=_("Combat Readiness state"),
            value=(combat_state['state'].replace('_', ' ').title()
                   if combat_state else _('Not registered')),
        )
        embed.add_field(
            name=_("Combat Readiness cycle"),
            value=str(combat_state['cycle_number']) if combat_state else '—',
        )
        embed.add_field(
            name=_("Valid AAR"),
            value='\n'.join(snapshot.aar_names) if snapshot.aar_names else _('None'),
            inline=False,
        )
        embed.add_field(
            name=_("Valid IFR"),
            value='\n'.join(snapshot.ifr_names) if snapshot.ifr_names else _('None'),
            inline=False,
        )
        if combat_state and combat_state['last_awarded_at']:
            embed.add_field(
                name=_("Combat Readiness last awarded"),
                value=str(combat_state['last_awarded_at']),
                inline=False,
            )
        if combat_state and combat_state['last_reset_at']:
            embed.add_field(
                name=_("Combat Readiness last reset"),
                value=str(combat_state['last_reset_at']),
                inline=False,
            )

        statistical_lines = []
        for rule_key, rule in self.stat_rules():
            progress = row['stat_progress'].get(rule_key)
            if not progress:
                statistical_lines.append(
                    f"**{self._award_label(rule_key, rule)}:** "
                    f"{row['stat_errors'].get(rule_key, _('Unavailable'))}"
                )
                continue

            scope_server = progress.get('scope_server')
            state_key = scoped_rule_state_key(
                rule_key, progress['campaign']['id'], scope_server
            )
            state = row['states'].get(state_key)
            state_label = (
                state['state'].replace('_', ' ').title()
                if state else _('Not registered')
            )
            metrics = cast(CampaignMetrics, progress['metrics'])
            if rule_key == 'air_force_cross':
                minimum_hours = int(rule.get('minimum_flight_hours', 1000))
                kd_ratio_over = float(rule.get('kd_ratio_over', 50))
                detail = (
                    f"{metrics.flight_hours:,.1f}/{minimum_hours:,}h; "
                    f"K/D {metrics.kd_ratio:,.2f} (>{kd_ratio_over:g})"
                )
            elif rule_key == 'aerial_achievement':
                required_kills = int(rule.get('air_kills', 50))
                detail = f"{metrics.air_kills:,}/{required_kills:,} A/A kills"
            elif rule_key == 'airmans_medal':
                hours_per_award = int(rule.get('hours_per_award', 150))
                next_cycle = int(state['cycle_number']) if state else 1
                detail = (
                    f"{metrics.cargo_hours:,.1f} cargo hours; "
                    f"next at {next_cycle * hours_per_award:,}h"
                )
            else:
                hours_per_award = int(rule.get('hours_per_award', 500))
                next_cycle = int(state['cycle_number']) if state else 1
                detail = (
                    f"{metrics.flight_hours:,.1f} flight hours; "
                    f"next at {next_cycle * hours_per_award:,}h"
                )
            statistical_lines.append(
                f"**{self._award_label(rule_key, rule)} "
                f"({'Server: ' + scope_server if scope_server else 'Campaign'}):** "
                f"{detail} — {state_label}"
            )

        if statistical_lines:
            embed.add_field(
                name=_("Statistical awards"),
                value='\n'.join(statistical_lines),
                inline=False,
            )

        if row['hardcore_streak'] is not None:
            hardcore_lines = [
                _("Server: **{}**").format(
                    row.get('hardcore_server', self.hardcore_server_selector())
                ),
                _("Current qualifying streak: **{} sessions**").format(
                    row['hardcore_streak']
                ),
            ]
            for rule_key, rule in self.hardcore_rules():
                rule_state = row['states'].get(rule_key)
                state_label = (rule_state['state'].replace('_', ' ').title()
                               if rule_state else _('Not registered'))
                hardcore_lines.append(
                    f"**{self._award_label(rule_key, rule)} "
                    f"({rule.get('sessions')}):** {state_label}"
                )
            embed.add_field(
                name=_("Hardcore milestones"),
                value='\n'.join(hardcore_lines),
                inline=False,
            )
        elif row['hardcore_error']:
            embed.add_field(
                name=_("Hardcore milestones"),
                value=_("Unavailable: {}").format(row['hardcore_error']),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @awardautomation.command(name="scan", description=_("Run the automatic-award check now"))
    @app_commands.guild_only()
    @utils.app_has_role('DCS Admin')
    @app_commands.describe(user=_("Optional pilot; leave empty to scan everyone"))
    async def automation_scan(
        self,
        interaction: discord.Interaction,
        user: discord.Member | None = None,
    ):
        await interaction.response.defer(ephemeral=True)
        ucid = None
        if user:
            ucid = await self.bot.get_ucid_by_member(user)
            if not ucid:
                await interaction.followup.send(
                    _("User {} is not linked to DCS.").format(user.display_name), ephemeral=True
                )
                return
        results = await self.scan(ucid)
        awarded = sum(result.action == CycleAction.AWARD for result in results)
        reset = sum(result.action == CycleAction.RESET for result in results)
        baselined = sum(result.action == CycleAction.BASELINE_LOCK for result in results)
        errors = [result.detail for result in results if result.detail]
        message = _(
            "Processed {processed} award check(s): {awarded} awarded, {reset} reset, "
            "{baselined} baselined."
        ).format(
            processed=len(results), awarded=awarded, reset=reset, baselined=baselined
        )
        if errors:
            message += _("\nWarnings: {}").format('; '.join(errors[:5]))
        await interaction.followup.send(message, ephemeral=True)


async def setup(bot: DCSServerBot):
    for plugin in ('mission', 'creditsystem', 'logbook'):
        if plugin not in bot.plugins:
            raise PluginRequiredError(plugin)
    await bot.add_cog(AwardAutomation(bot))
