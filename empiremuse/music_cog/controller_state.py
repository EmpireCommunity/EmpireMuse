"""Estado compartilhado pelas etapas de `Music.player_controller`."""


class ControllerState:
    """Variáveis que passam de uma etapa para outra (antes, locais de `player_controller`)."""

    __slots__ = (
        "author",
        "bot",
        "channel",
        "choices",
        "cmd",
        "cmd_kwargs",
        "control",
        "embed",
        "fav_name",
        "info",
        "interaction",
        "modal_components",
        "msg",
        "node",
        "player",
        "seek_status",
        "select_type",
        "track",
        "tr",
        "url",
        "user_data",
        "vc",
        "vc_id",
    )

    def __init__(self, *, interaction, control):
        self.interaction = interaction
        self.control = control
        self.author = None
        self.bot = None
        self.channel = None
        self.choices = None
        self.cmd = None
        self.cmd_kwargs = None
        self.embed = None
        self.fav_name = None
        self.info = None
        self.modal_components = None
        self.msg = None
        self.node = None
        self.player = None
        self.seek_status = None
        self.select_type = None
        self.track = None
        self.tr = None
        self.url = None
        self.user_data = None
        self.vc = None
        self.vc_id = None
