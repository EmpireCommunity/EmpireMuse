"""O que toda skin do EmpireMuse tem em comum: o formato que o carregador espera e as funções pequenas de apoio.

O ``client.load_muse_skins`` importa ``utils/music/skins/muse/<id>.py`` só para ids de ``rules/skins.JUKEBOX_IDS`` e chama
``load()``; a MESMA instância vale nos dois modos (normal e canal da Jukebox), então ``setup_features`` nunca muda
``player.static``: quem decide o modo é o player.
"""

from __future__ import annotations

from typing import Any

from empiremuse.utils.branding import asset_url

PREVIEW_ASSET = "skin_muse_{skin}_preview.png"  # o caminho do registro (``rules/skins._preview``); o arquivo vem no S6


def preview_url(skin_id: str) -> str:
    """A URL da prévia, ou vazio enquanto o asset não existe (as prévias chegam com o ``/skins``, S6)."""
    try:
        return asset_url(PREVIEW_ASSET.format(skin=skin_id))
    except ValueError:
        return ""


def playing_on_bot(player: Any) -> int:
    """Quantos players deste bot estão tocando agora (para o intervalo da barra em texto)."""
    players = getattr(getattr(player.bot, "music", None), "players", None) or {}
    return sum(
        1 for p in players.values() if getattr(p, "current", None) is not None and not getattr(p, "paused", False)
    )


def base_features(player: Any, *, auto_update: int, mini_queue: bool) -> None:
    player.mini_queue_feature = mini_queue
    player.controller_mode = True
    player.auto_update = auto_update
    player.hint_rate = player.bot.config["HINT_RATE"]
