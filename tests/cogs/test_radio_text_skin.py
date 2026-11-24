"""A skin ``text`` da rádio (Rádio Texto v2, S7) e o despacho do painel por skin (``panel.skin_payload_async``).

Riscos cobertos: texto de fora publicado no embed (um escape só, sem link nem menção, nas duas fontes: guardado escapado na
ao vivo, cru na personalizada), a skin de texto fora da vaga de render e trocando o anexo do visor, e o ``render=`` de quem
chama (o Retrato) recebendo ``(snapshot, tr)``.
"""

import pytest

from empiremuse.i18n import get_i18n
from empiremuse.utils.radio.snapshot import StationOption
from tests.support.radio_snapshot import snapshot

pytestmark = pytest.mark.critical

PT = get_i18n().for_locale("pt-BR")
HOSTILE = "@everyone [x](https://evil.example) <@123>"


def text_of(payload):
    embed = payload["embeds"][0]
    return "\n".join(str(part) for part in (embed.title, embed.description, embed.author.name, embed.footer.text))


class NoSlot:
    async def run(self, *args, **kwargs):
        raise AssertionError("a skin de texto não usa a vaga de render")


class InlineSlot:
    def __init__(self):
        self.calls = []

    async def run(self, fn, *args, label="render", wait_s=None):
        self.calls.append(label)
        return fn(*args)


@pytest.mark.parametrize(
    ("custom", "name", "shown"),
    [
        (False, "Rock\\_Br", "Rock\\_Br"),  # ao vivo: guardado escapado, sai com um escape só
        (True, "Rock_Br", "Rock\\_Br"),  # personalizada: cru, escapado na saída
    ],
)
def test_outside_text_gets_one_escape_and_no_link_or_mention(custom, name, shown):
    from empiremuse.utils.radio.panel import text_payload

    snap = snapshot(
        look="text",
        station_name=name,
        now_title=f"{HOSTILE} {name}",
        now_artist=HOSTILE,
        filter_path=(HOSTILE,),
        stations=tuple(StationOption(i, f"id{i}", f"{HOSTILE} {name}", 64, "mp3", "good") for i in range(1, 6)),
        recent_titles=() if custom else ((1_700_000_000.0, f"{HOSTILE} {name}"),),  # o histórico só existe na ao vivo
    )
    text = text_of(text_payload(PT, snap, filter_options=[], custom=custom, channel=HOSTILE, at=0))

    assert shown in text and "\\\\" not in text  # título, faixa, histórico e estações: nenhum escape duplo
    if not custom:
        assert PT("radio.text.history") in text
    for alive in ("@everyone", "<@123>", "https://", "](http"):
        assert alive not in text


@pytest.mark.parametrize(("state", "song_shown"), [("live", True), ("muted", False), ("offline", False)])
def test_the_song_only_shows_while_live(state, song_shown):
    from empiremuse.utils.radio.panel import text_payload

    snap = snapshot(look="text", state=state, now_title="Céu de Neon", retry_seconds=30)
    assert ("Céu de Neon" in text_of(text_payload(PT, snap, filter_options=[], at=0))) is song_shown


@pytest.mark.asyncio
async def test_the_text_skin_skips_the_render_slot_and_replaces_the_visor_attachment():
    from empiremuse.utils.radio.panel import LOGO_NAME, skin_payload_async, visor_payload

    snap = snapshot(look="text", logo=b"logo")
    payload = await skin_payload_async(PT, snap, filter_options=[], slot=NoSlot())

    assert payload["file"].filename == LOGO_NAME and payload["file"].fp.read() == b"logo"
    assert payload["embeds"][0].thumbnail.url == f"attachment://{LOGO_NAME}"
    visor = visor_payload(PT, snap, filter_options=[], picture=b"x")
    ids = [[c.custom_id for c in row.children] for row in payload["components"]]
    assert ids == [[c.custom_id for c in row.children] for row in visor["components"]], "os mesmos controles do visor"


@pytest.mark.asyncio
async def test_image_skins_draw_with_the_callers_render_in_the_slot():
    from empiremuse.utils.radio.panel import VISOR_NAME, skin_payload_async

    snap = snapshot(look="portrait_black")
    seen = []

    def render(*args):
        seen.append(args)
        return b"png"

    slot = InlineSlot()
    payload = await skin_payload_async(PT, snap, filter_options=[], slot=slot, render=render)

    assert seen == [(snap, PT)] and slot.calls == ["visor"]
    assert payload["file"].filename == VISOR_NAME and payload["file"].fp.read() == b"png"
