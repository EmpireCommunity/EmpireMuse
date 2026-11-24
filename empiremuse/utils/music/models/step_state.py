"""Estado compartilhado pelas etapas dos métodos longos do player."""


class NpState:
    """Variáveis que passam de uma etapa para outra (antes, locais de `invoke_np`)."""

    __slots__ = (
        "data",
        "force",
        "interaction",
        "published",
        "rpc_update",
        "superseded",
    )

    def __init__(self, *, force, interaction, rpc_update):
        self.superseded = False  # o pedido saiu sem publicar por ser superado (cartão antigo) ou por trocar de faixa
        self.published = False  # a edição ou o envio da mensagem terminou sem falha (D10: o que foi desenhado vale)
        self.force = force
        self.interaction = interaction
        self.rpc_update = rpc_update
        self.data = None


class AutoqueueState:
    """Variáveis que passam de uma etapa para outra (antes, locais de `get_autoqueue_tracks`)."""

    __slots__ = (
        "exception",
        "t",
        "track",
        "track_data",
        "tracks",
        "tracks_search",
        "tracks_ytsearch",
    )

    def __init__(self):
        self.exception = None
        self.t = None
        self.track = None
        self.track_data = None
        self.tracks = None
        self.tracks_search = None
        self.tracks_ytsearch = None


class ProcessNextState:
    """Variáveis que passam de uma etapa para outra (antes, locais de `_process_next`)."""

    __slots__ = (
        "encoded_track",
        "error_code",
        "force_np",
        "inter",
        "start_position",
        "track",
    )

    def __init__(self, *, start_position, inter, force_np):
        self.start_position = start_position
        self.inter = inter
        self.force_np = force_np
        self.encoded_track = None
        self.error_code = None
        self.track = None


class ResolveTrackState:
    """Variáveis que passam de uma etapa para outra (antes, locais de `resolve_track`)."""

    __slots__ = (
        "cached",
        "check_duration",
        "exceptions",
        "first_result",
        "selected_result",
        "selected_track",
        "tracks",
    )

    def __init__(self, *, check_duration):
        self.cached = None
        self.check_duration = check_duration
        self.exceptions = []
        self.first_result = None
        self.selected_result = None
        self.selected_track = None
        self.tracks = []
