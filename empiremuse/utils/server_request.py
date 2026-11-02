# -*- coding: utf-8 -*-
"""Compatibilidade temporária até o merge de ``refactor/nomes-radios-bot``."""

from empiremuse.utils.server_jukebox import JukeboxConfig as RequestConfig
from empiremuse.utils.server_jukebox import ServerJukebox as ServerRequest

__all__ = ["RequestConfig", "ServerRequest"]
