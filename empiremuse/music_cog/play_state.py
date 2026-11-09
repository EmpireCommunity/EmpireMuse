"""Estado compartilhado pelas etapas do comando `/play`."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import disnake

    from empiremuse.utils.others import CustomContext


class PlayState:
    """Variáveis que passam de uma etapa do `/play` para outra (antes, locais de `Music.play`)."""

    __slots__ = (
        "attachment",
        "author",
        "bot",
        "channel",
        "embed",
        "embed_description",
        "emoji",
        "ephemeral",
        "fav_opts",
        "fav_slashcmd",
        "force_play",
        "guild",
        "guild_data",
        "image_file",
        "info",
        "inter",
        "is_pin",
        "loadtype",
        "log_text",
        "manual_bot_choice",
        "manual_selection",
        "matches",
        "menu",
        "message_inter",
        "mix",
        "modal_message_id",
        "msg",
        "node",
        "options",
        "opts",
        "original_bot",
        "original_query",
        "player",
        "pos_txt",
        "position",
        "profile_avatar",
        "query",
        "queue_loaded",
        "reg_query",
        "savequeue_slashcmd",
        "sc_match",
        "selected_title",
        "server",
        "source",
        "track_url",
        "tracks",
        "txt",
        "urls",
        "user_data",
        "user_read_at",
        "voice_channel",
        "warn_message",
    )

    def __init__(
        self,
        *,
        inter: "disnake.ApplicationCommandInteraction | CustomContext",
        query: str,
        position: int,
        force_play: str,
        mix: "str | bool",
        manual_selection: bool,
        options: "str | bool",
        server: "str | None",
        manual_bot_choice: str,
    ) -> None:
        self.inter = inter
        self.query = query
        self.position = position
        self.force_play = force_play
        self.mix = mix
        self.manual_selection = manual_selection
        self.options = options
        self.server = server
        self.manual_bot_choice = manual_bot_choice
        self.attachment = None
        self.author = None
        self.bot = None
        self.channel = None
        self.embed = None
        self.embed_description = None
        self.emoji = None
        self.ephemeral = None
        self.fav_opts = None
        self.fav_slashcmd = None
        self.guild = None
        self.guild_data = None
        self.image_file = None
        self.info = None
        self.is_pin = None
        self.loadtype = None
        self.log_text = None
        self.matches = None
        self.menu = None
        self.message_inter = None
        self.modal_message_id = None
        self.msg = None
        self.node = None
        self.opts = None
        self.original_bot = None
        self.original_query = None
        self.player = None
        self.pos_txt = None
        self.profile_avatar = None
        self.queue_loaded = None
        self.reg_query = None
        self.savequeue_slashcmd = None
        self.sc_match = None
        self.selected_title = None
        self.source = None
        self.track_url = None
        self.tracks = None
        self.txt = None
        self.urls = None
        self.user_data = None
        self.user_read_at = None
        self.voice_channel = None
        self.warn_message = None
