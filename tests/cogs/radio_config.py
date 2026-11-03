"""Rádios JÁ CONFIGURADAS para os testes (o que o ``/radio setup`` deixa gravado): canal, bot, tipo e estação, ainda desligadas."""

from empiremuse.rules.radio import check_radio, radio_id

CUSTOM_STATION = "0123456789abcdef01234567"
LIVE_STATION = "11111111-2222-3333-4444-555555555555"


async def configure(hub, *, guild_id=500, channel_id=900, bot_id=2, kind="custom", source=None):
    """Grava (ou atualiza, se desligada) a rádio do bot neste servidor. ``source=None`` põe a estação padrão do tipo; ``{}`` deixa sem estação."""
    if source is None:
        source = {"station": CUSTOM_STATION} if kind == "custom" else {"station": LIVE_STATION, "reserve": None}
    wanted = check_radio(
        {"_id": radio_id(bot_id, guild_id), "channel_id": str(channel_id), "kind": kind, "source": source}
    )
    existing = await hub.store.get(wanted.id)
    if existing is None:
        return await hub.store.create(wanted)
    return await hub.store.save_config(wanted, expected_rev=existing.rev)
