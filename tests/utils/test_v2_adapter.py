"""O adaptador do Components V2 (``utils/music/v2``): uma mensagem V2 nunca recebe ``content`` nem ``embeds`` (400 no Discord) e
uma V1 segue como está. Mensagens falsas, sem rede."""

from types import SimpleNamespace

import disnake
import pytest

from empiremuse.utils.music import v2

pytestmark = pytest.mark.critical


def message(*, v2_flag: bool, edits: list | None = None):
    flags = disnake.MessageFlags()
    flags.is_components_v2 = v2_flag

    async def edit(**kwargs):
        edits.append(kwargs)

    return SimpleNamespace(flags=flags, edit=edit)


def embed_kwargs():
    embed = disnake.Embed(title="Título", url="https://music.example/x", description="corpo", color=0x8B5CF6)
    embed.set_author(name="TOCANDO")
    embed.set_footer(text="dica")
    embed.set_thumbnail(url="https://music.example/c.png")
    select = disnake.ui.Select(custom_id="idle", options=[disnake.SelectOption(label="a", value="a")])
    buttons = [disnake.ui.Button(custom_id=f"b{n}") for n in range(6)]
    return {"content": "oi", "embed": embed, "components": [select, *buttons], "attachments": []}


def test_a_v1_message_keeps_its_payload_as_it_is():
    kwargs = embed_kwargs()
    assert v2.adapt_for(message(v2_flag=False), kwargs) == kwargs


def test_a_partial_message_without_flags_counts_as_v1():
    assert v2.is_v2(SimpleNamespace()) is False
    assert v2.adapt_for(SimpleNamespace(), {"embed": disnake.Embed(description="x")})["embed"] is not None


def test_a_v2_message_never_gets_content_or_embeds():
    out = v2.adapt_for(message(v2_flag=True), embed_kwargs())
    assert out["content"] is None
    assert out["embeds"] == []
    assert "embed" not in out and "view" not in out
    assert out["attachments"] == []  # o resto do pedido passa
    flat = [item.to_component_dict() for item in out["components"]]
    assert flat[0]["type"] == 10  # o content vira um TextDisplay antes do card
    container = flat[1]
    assert container["type"] == 17
    assert container["accent_color"] == 0x8B5CF6
    kinds = [child["type"] for child in container["components"]]
    assert kinds[0] == 9  # a miniatura vira o acessório de uma Section
    assert kinds[1:] == [10, 10, 10, 1, 1, 1]  # título, corpo e rodapé; 1 menu e 5 + 1 botões


def test_loose_buttons_become_rows_of_five_and_a_select_has_its_own_row():
    select = disnake.ui.Select(custom_id="s", options=[disnake.SelectOption(label="a", value="a")])
    rows = v2.components_to_rows([*[disnake.ui.Button(custom_id=f"b{n}") for n in range(7)], select])
    assert [len(row.children) for row in rows] == [5, 2, 1]


def test_the_text_never_goes_over_the_4000_character_limit():
    big = disnake.Embed(description="x" * 9000)
    out = v2.legacy_to_v2({"embeds": [big, disnake.Embed(description="y" * 500)]})
    total = sum(
        len(child["content"])
        for container in out["components"]
        for child in container.to_component_dict()["components"]
        if child["type"] == 10
    )
    assert total <= v2.MAX_TEXT


@pytest.mark.asyncio
async def test_edit_message_converts_for_v2_and_leaves_v1_alone():
    edits: list = []
    await v2.edit_message(message(v2_flag=True, edits=edits), embed=disnake.Embed(description="a"))
    await v2.edit_message(message(v2_flag=False, edits=edits), embed=disnake.Embed(description="a"))
    assert edits[0]["embeds"] == [] and edits[0]["content"] is None
    assert isinstance(edits[1]["embed"], disnake.Embed)


@pytest.mark.asyncio
async def test_an_interaction_on_a_v2_message_edits_with_a_container():
    sent: list = []

    async def edit_message(**kwargs):
        sent.append(kwargs)

    interaction = SimpleNamespace(
        message=message(v2_flag=True), response=SimpleNamespace(is_done=lambda: False, edit_message=edit_message)
    )
    await v2.edit_from_interaction(interaction, embed=disnake.Embed(description="ocioso"), content=None)
    assert sent[0]["embeds"] == [] and sent[0]["content"] is None
    assert sent[0]["components"][0].to_component_dict()["type"] == 17


@pytest.mark.regression
def test_two_embeds_with_many_fields_stay_inside_the_forty_components():
    embeds = [disnake.Embed(description="d") for _ in range(2)]
    for embed in embeds:
        for n in range(20):
            embed.add_field(name=f"f{n}", value="v")
    data = v2.legacy_to_v2({"embeds": embeds, "components": []})
    assert sum(v2.count_components(item) for item in data["components"]) <= v2.MAX_COMPONENTS


@pytest.mark.regression
def test_many_embeds_with_images_and_many_controls_still_end_inside_the_forty_components():
    embeds = []
    for n in range(10):
        embed = disnake.Embed(description=f"d{n}")
        embed.set_image(url="https://exemplo.invalid/i.png")
        embeds.append(embed)
    buttons = [disnake.ui.Button(label=str(n), custom_id=f"b{n}") for n in range(25)]
    data = v2.legacy_to_v2({"embeds": embeds, "components": buttons})
    assert sum(v2.count_components(item) for item in data["components"]) <= v2.MAX_COMPONENTS
