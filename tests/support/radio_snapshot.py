"""Retrato de rádio ao vivo de exemplo para os testes do visor e da vaga de render (``RadioPanelSnapshot`` completo)."""


def snapshot(**overrides):
    from empiremuse.utils.radio.snapshot import RadioPanelSnapshot, StationOption

    window = tuple(StationOption(i, f"id{i}", f"Estação {i}", 64 * i, "mp3", "good") for i in range(1, 6))
    base = dict(
        radio_id="1:100",
        skin="white",
        station_name="Estação 3",
        country="Brasil",
        codec="mp3",
        bitrate_kbps=192,
        logo=None,
        quality="ok",
        now_title="Música",
        now_artist=None,
        state="live",
        reserve_number=None,
        volume=150,
        started_at=None,
        on_air_minutes=7,
        filter_path=("América do Sul", "Brasil", "SP", "Campinas"),
        list_kind="stations",
        position=3,
        total=40,
        stations=window,
        current_index=2,
        favorite=True,
        favorites_full=False,
        retry_seconds=None,
    )
    base.update(overrides)
    return RadioPanelSnapshot(**base)
