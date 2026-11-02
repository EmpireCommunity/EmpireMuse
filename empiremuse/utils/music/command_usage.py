"""O texto de uso (``cmd.<id>.usage``) de um comando de prefixo no idioma do tradutor."""

from __future__ import annotations

from typing import Any, Optional


def command_usage(tr: Any, cmd: Any, prefix: str, bot_id: Optional[int] = None, bot_name: str = "") -> Optional[str]:
    """O uso do comando com ``{prefix}``, ``{cmd}`` e ``{parent}`` preenchidos; ``None`` se não há texto de uso.

    Lê o catálogo e cai no ``usage`` do decorador, para o comando que ainda não tem a chave.
    """
    from empiremuse.cogs.help_cog import catalog_id

    from empiremuse.i18n.service import get_i18n

    key = f"cmd.{catalog_id(cmd)}.usage"
    if key in get_i18n().reference:
        text = tr(key, prefix=prefix, cmd=cmd.name, parent=cmd.full_parent_name, lt="<", gt=">")
    else:
        if not cmd.usage:
            return None
        text = (
            cmd.usage.replace("{prefix}", prefix).replace("{cmd}", cmd.name).replace("{parent}", cmd.full_parent_name)
        )
    if bot_id is not None:
        text = text.replace(f"<@!{bot_id}>", f"@{bot_name}").replace(f"<@{bot_id}>", f"@{bot_name}")
    return text
