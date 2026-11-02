# -*- coding: utf-8 -*-
import logging
import os
import traceback
from typing import Union, Optional

import disnake
from disnake.ext import commands
from disnake.utils import escape_mentions

from empiremuse.rules.error_code import safe_error_code
from empiremuse.utils.music.command_usage import command_usage
from empiremuse.utils.music.converters import time_format, perms_translations
from empiremuse.utils.music.guild_text import guild_text_sync
from empiremuse.wavelink import WavelinkException, TrackNotFound, MissingSessionID

logger = logging.getLogger("empiremuse.errors")


def error_code_text(code: str) -> str:
    """Linha com o código do erro para o usuário (vazia se não houve código)."""
    return f"\n\nCódigo do erro: `{code}` (informe ao suporte se precisar de ajuda)." if code else ""


def code_suffix(code: str) -> str:
    """Código do erro numa frase curta, para mensagens de uma linha (vazio se não houve código)."""
    return f" (código do erro: `{code}`)" if code else ""


def log_unexpected_error(log: logging.Logger, error: BaseException, what: str) -> str:
    """Registra o erro inesperado com o traceback completo (só no log do servidor) e devolve o código dele.

    O código (``ERR-…``) vai na mensagem do log e na mensagem mostrada a quem usou; ``""`` se não foi possível gerar.
    """
    code = safe_error_code()
    error = getattr(error, "original", error)
    log.error(
        "Erro inesperado em %s%s",
        what,
        f" [{code}]" if code else "",
        exc_info=(type(error), error, error.__traceback__),
        # O agregador usa o código para apontar o traceback já impresso.
        extra={"error_code": code} if code else None,
    )
    return code


class PoolException(commands.CheckFailure):
    pass


class ArgumentParsingError(commands.CommandError):
    def __init__(self, message):
        super().__init__(escape_mentions(message))


class GenericError(commands.CheckFailure):
    def __init__(
        self,
        text: str,
        *,
        self_delete: int = None,
        delete_original: Optional[int] = None,
        components: list = None,
        error: str = None,
    ):
        self.text = text
        self.self_delete = self_delete
        self.delete_original = delete_original
        self.components = components
        self.error = error

    def __repr__(self):
        return disnake.utils.escape_markdown(self.text)

    def __str__(self):
        return disnake.utils.escape_markdown(self.text)


class EmptyFavIntegration(commands.CheckFailure):
    pass


class MissingSpotifyClient(commands.CheckFailure):
    pass


class NoPlayer(commands.CheckFailure):
    pass


class NoVoice(commands.CheckFailure):
    pass


class MissingVoicePerms(commands.CheckFailure):
    def __init__(self, voice_channel: Union[disnake.VoiceChannel, disnake.StageChannel]):
        self.voice_channel = voice_channel


class DiffVoiceChannel(commands.CheckFailure):
    pass


class NoSource(commands.CheckFailure):
    pass


class NotDJorStaff(commands.CheckFailure):
    pass


class NotRequester(commands.CheckFailure):
    pass


class YoutubeSourceDisabled(commands.CheckFailure):
    pass


def _usage_text(translate, ctx) -> Optional[str]:
    """O ``Como usar`` do comando de prefixo (catálogo ``cmd.<id>.usage``), ou ``None`` se ele não tem uso."""
    command = getattr(ctx, "command", None)
    if command is None:
        return None
    prefix = ctx.prefix if str(ctx.me.id) not in ctx.prefix else f"@{ctx.me.display_name} "
    return command_usage(translate, command, prefix)


def parse_error(
    ctx: Union[disnake.ApplicationCommandInteraction, commands.Context, disnake.MessageInteraction],
    error: Exception,
    **kwargs,
):
    error_txt = None

    kill_process = False

    mention_author = False

    components = []

    send_error = False

    error = getattr(error, "original", error)

    guild = getattr(ctx, "guild", None)
    translate = kwargs.get("tr") or (lambda key, **values: guild_text_sync(guild, key, **values))
    t = lambda key, **values: translate(f"legacy.errors.{key}", **values)  # noqa: E731

    if isinstance(error, NotDJorStaff):
        error_txt = t("not_dj_or_staff")

    elif isinstance(error, MissingVoicePerms):
        error_txt = t("missing_voice_perms", channel=error.voice_channel.mention)

    elif isinstance(error, commands.NotOwner):
        error_txt = t("not_owner")

    elif isinstance(error, commands.BotMissingPermissions):
        error_txt = t(
            "bot_missing_permissions",
            perms=", ".join(perms_translations.get(perm, perm) for perm in error.missing_permissions),
        )

    elif isinstance(error, commands.MissingPermissions):
        error_txt = t(
            "missing_permissions",
            perms=", ".join(perms_translations.get(perm, perm) for perm in error.missing_permissions),
        )

    elif isinstance(error, GenericError):
        error_txt = error.text
        components = error.components
        if error.text:
            send_error = True

    elif isinstance(error, NotRequester):
        error_txt = t("not_requester")

    elif isinstance(error, DiffVoiceChannel):
        error_txt = t("diff_voice_channel")

    elif isinstance(error, NoSource):
        error_txt = t("no_source")

    elif isinstance(error, NoVoice):
        error_txt = t("no_voice")

    elif isinstance(error, NoPlayer):
        try:
            error_txt = t("no_player_in_channel", channel=ctx.author.voice.channel.mention)
        except AttributeError:
            error_txt = t("no_player")

    elif isinstance(error, (commands.UserInputError, commands.MissingRequiredArgument)) and (
        usage := _usage_text(translate, ctx)
    ):
        error_txt = t("wrong_usage") + t("how_to_use", usage=str(usage))

    elif isinstance(error, MissingSpotifyClient):
        error_txt = t("no_spotify")

    elif isinstance(error, commands.NoPrivateMessage):
        error_txt = t("no_private_message")

    elif isinstance(error, MissingSessionID):
        error_txt = t("node_disconnected", node=error.node.identifier)

    elif isinstance(error, commands.CommandOnCooldown):
        remaing = max(int(error.retry_after), 1)
        error_txt = t("cooldown", time=time_format(remaing * 1000, use_names=True))

    elif isinstance(error, EmptyFavIntegration):
        error_txt = t("empty_fav_button") if isinstance(ctx, disnake.MessageInteraction) else t("empty_fav_command")

        mention_author = True

        components = [
            disnake.ui.Button(label=t("open_fav_manager"), custom_id="musicplayer_fav_manager", emoji="⭐"),
        ]

    elif isinstance(error, commands.MaxConcurrencyReached):
        times = t("times_prefix", number=error.number) if error.number > 1 else ""
        buckets = {
            commands.BucketType.member: "bucket_member",
            commands.BucketType.guild: "bucket_guild",
            commands.BucketType.user: "bucket_user",
            commands.BucketType.channel: "bucket_channel",
            commands.BucketType.category: "bucket_category",
            commands.BucketType.role: "bucket_role",
            commands.BucketType.default: "bucket_default",
        }
        error_txt = translate(
            "legacy.errors.concurrency",
            count=error.number,
            user=ctx.author.mention,
            what=t(buckets[error.per], times=times),
        )

    elif isinstance(error, TrackNotFound):
        error_txt = t("track_not_found")

    elif isinstance(error, YoutubeSourceDisabled):
        link = f"[{t('click_here')}](<https://support.google.com/youtube/thread/269521462/enforcement-on-third-party-apps?hl=en>)"
        error_txt = t("youtube_disabled", link=link)

    elif isinstance(error, WavelinkException):
        wave_error = str(error)
        lowered = wave_error.lower()
        if "Unknown file format" in wave_error:
            error_txt = t("unsupported_link")
        elif "No supported audio format" in wave_error:
            error_txt = t("unsupported_format")
        elif "This video is not available" in wave_error:
            error_txt = t("video_unavailable")
        elif "This playlist type is unviewable" in wave_error:
            error_txt = t("playlist_bad_param")
        elif "The playlist does not exist" in wave_error:
            error_txt = t("playlist_missing")
        elif (
            "not made this video available in your country" in lowered
            or "who has blocked it in your country on copyright grounds" in lowered
        ):
            error_txt = t("region_blocked")

    full_error_txt = ""

    if not error_txt:
        full_error_txt = "".join(traceback.format_exception(type(error), error, error.__traceback__))
        if not kwargs.get("no_log"):
            logger.error("Erro inesperado", exc_info=(type(error), error, error.__traceback__))
    elif send_error:
        full_error_txt = "".join(traceback.format_exception(type(error), error, error.__traceback__))

    return error_txt, full_error_txt, kill_process, components, mention_author
