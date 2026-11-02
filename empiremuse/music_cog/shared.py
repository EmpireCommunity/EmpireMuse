# -*- coding: utf-8 -*-
"""Constantes de corpo de classe usadas por mais de um mixin de ``Music`` (ver ``empiremuse/cogs/music.py``)."""

import logging
import re
from urllib.parse import urlparse

import disnake
from disnake.ext import commands

from empiremuse.i18n import localized
from empiremuse.utils.others import CommandArgparse

# Mesmo nome do logger de ``empiremuse/cogs/music.py``: o que os mixins registram continua nele.
logger = logging.getLogger("empiremuse.cogs.music")

EMOJI = "🎶"
NAME = "Music"
DESC_PREFIX = f"[{EMOJI} {NAME}] | "

remove_mc = commands.MaxConcurrency(1, per=commands.BucketType.guild, wait=False)
queue_manipulation_cd = commands.CooldownMapping.from_cooldown(2, 15, commands.BucketType.guild)
case_sensitive_args = CommandArgparse()
case_sensitive_args.add_argument(
    "-casesensitive",
    "-cs",
    action="store_true",
    help="legacy.flag.casesensitive",
)

play_cd = commands.CooldownMapping.from_cooldown(3, 12, commands.BucketType.member)
play_mc = commands.MaxConcurrency(1, per=commands.BucketType.member, wait=False)
playlist_opts = [
    disnake.OptionChoice(localized("cmd.play.opt.options.choice.shuffle"), "shuffle"),
    disnake.OptionChoice(localized("cmd.play.opt.options.choice.reversed"), "reversed"),
]

sc_recommended = re.compile(r"https://soundcloud\.com/.*/recommended$")
sc_profile_regex = re.compile(r"<?https://soundcloud\.com/[a-zA-Z0-9_-]+>?$")

DISCORD_CDN_HOSTS = frozenset({"cdn.discordapp.com", "media.discordapp.net"})


DISCORD_ATTACHMENT_PATH_PREFIXES = ("/attachments/", "/ephemeral-attachments/")


def is_discord_attachment_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        hostname = (parsed.hostname or "").rstrip(".")
    except ValueError:
        return False
    return hostname in DISCORD_CDN_HOSTS and parsed.path.startswith(DISCORD_ATTACHMENT_PATH_PREFIXES)
