from typing import Type

from core import Plugin, TEventListener
from services.bot import DCSServerBot

from .listener import CreditsEventListener


class Credits(Plugin[CreditsEventListener]):
    """
    JSH Credits.

    Holds every credit a player earns in a sortie until they land at a friendly
    airbase or FARP. Points are dropped if the player crashes, ejects, dies,
    changes slot or disconnects first.

    This plugin is the only thing that should write credits. CreditSystem stays
    the ledger (balances, audit log, achievements, Discord roles); SlotBlocking
    payback must be off.
    """

    def __init__(self, bot: DCSServerBot, eventlistener: Type[TEventListener] = None):
        super().__init__(bot, eventlistener)


async def setup(bot: DCSServerBot):
    await bot.add_cog(Credits(bot, CreditsEventListener))
