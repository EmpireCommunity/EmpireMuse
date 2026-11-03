"""Estado compartilhado pelas etapas dos pedidos de música na Jukebox."""


class JukeboxParseState:
    """Variáveis que passam de uma etapa para outra (antes, locais de ``parse_jukebox_request``)."""

    __slots__ = (
        "attachment",
        "components",
        "data",
        "destroy_message",
        "embed",
        "message",
        "player",
        "response",
        "source",
        "text_channel",
        "tracks",
    )

    def __init__(self, *, message, text_channel, data, response, attachment, source):
        self.message = message
        self.text_channel = text_channel
        self.data = data
        self.response = response
        self.attachment = attachment
        self.source = source
        self.components = None
        self.destroy_message = None
        self.embed = None
        self.player = None
        self.tracks = None


class JukeboxState:
    """Variáveis que passam de uma etapa para outra (antes, locais de ``on_jukebox_message``)."""

    __slots__ = (
        "ctx",
        "data",
        "error_code",
        "has_exception",
        "message",
        "msg",
        "player",
        "source",
        "text_channel",
    )

    def __init__(self, *, ctx, message):
        self.ctx = ctx
        self.message = message
        self.data = None
        self.error_code = None
        self.has_exception = None
        self.msg = None
        self.player = None
        self.source = None
        self.text_channel = None
