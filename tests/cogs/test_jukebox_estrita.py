"""Jukebox (item 8, J1): modo estrito, painel apagado volta na hora, fila e gênero do painel, volume e progresso.

A decisão do modo estrito é pura (``tests/rules/test_request_panel.py``); aqui está a ligação: o ``song_requests`` real apaga antes de
atender, o ``RequestPanel`` real recupera uma vez só e o painel mostra o que deve sem vazar nada de call privada.
"""

import ast
import asyncio
import pathlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs import music
from empiremuse.music_cog import panel_interactions
from empiremuse.rules.request_panel import GENRE_SELECT_ID, QUEUE_BUTTON_ID, PanelEntry, genre_for_key, genre_key
from empiremuse.utils import request_panel as panel_module
from empiremuse.utils.music.interactions import SelectInteraction
from empiremuse.utils.music.interactions.request_panel import render_panel
from tests.cogs.test_canal_unico import GID, make_bot, panel_world
from tests.cogs.test_mensagens_sem_excecao import montar_pedido
from tests.i18n.test_player_texts import EN, PT

# ---------------------------------------------------------------------- modo estrito no song_requests real


async def pedir(monkeypatch, *, estrito, mencoes=(), autor_bot=False, comando=None, store=None):
    classe, self_, ctx, mensagem, _envios, _ = montar_pedido(monkeypatch, RuntimeError("não deve chegar aqui"))
    ordem: list[str] = []
    self_.delete_message = AsyncMock(side_effect=lambda *a, **k: ordem.append("apagou"))
    self_.parse_song_request = AsyncMock(side_effect=lambda *a, **k: ordem.append("atendeu"))
    settings = AsyncMock(return_value={"request_channel_strict": estrito}) if store is None else store
    self_.bot.pool.servers.settings = settings
    mensagem.mentions = list(mencoes)
    mensagem.author.bot = autor_bot
    ctx.command = comando
    await classe.song_requests(self_, ctx, mensagem)
    return ordem


class TestModoEstritoNoCanal:
    @pytest.mark.asyncio
    async def test_a_typed_name_is_deleted_before_it_is_served(self, monkeypatch):
        assert await pedir(monkeypatch, estrito=True) == ["apagou", "atendeu"]

    @pytest.mark.asyncio
    async def test_a_message_with_a_mention_is_deleted_and_never_served(self, monkeypatch):
        assert await pedir(monkeypatch, estrito=True, mencoes=[object()]) == ["apagou"]

    @pytest.mark.asyncio
    async def test_without_the_strict_mode_a_mention_is_still_ignored_untouched(self, monkeypatch):
        assert await pedir(monkeypatch, estrito=False, mencoes=[object()]) == []

    @pytest.mark.asyncio
    async def test_without_the_strict_mode_nothing_is_deleted_ahead_of_the_request(self, monkeypatch):
        assert await pedir(monkeypatch, estrito=False) == ["atendeu"]

    @pytest.mark.asyncio
    async def test_a_message_outside_the_request_channel_never_reads_the_server_settings(self, monkeypatch):
        """Este listener roda em toda mensagem de todo servidor: a configuração só é lida no canal de pedidos."""
        classe, self_, ctx, mensagem, _envios, _ = montar_pedido(monkeypatch, RuntimeError("não deve chegar aqui"))
        mensagem.channel.id = 999  # o canal de pedidos do harness é o 55
        self_.bot.pool.servers.settings = AsyncMock(return_value={"request_channel_strict": True})

        await classe.song_requests(self_, ctx, mensagem)

        self_.bot.pool.servers.settings.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_audio_file_without_text_is_not_deleted_ahead_of_the_download(self, monkeypatch):
        classe, self_, ctx, mensagem, _envios, _ = montar_pedido(monkeypatch, RuntimeError("não deve chegar aqui"))
        ordem: list[str] = []
        self_.bot.pool.servers.settings = AsyncMock(return_value={"request_channel_strict": True})
        self_.delete_message = AsyncMock(side_effect=lambda *a, **k: ordem.append("apagou"))
        self_.parse_song_request = AsyncMock(side_effect=lambda *a, **k: ordem.append("atendeu"))
        mensagem.content = ""
        mensagem.attachments = [SimpleNamespace(size=10, content_type="audio/mpeg", url="https://cdn.invalid/a.mp3")]
        self_.audio_formats = ("audio/mpeg",)

        await classe.song_requests(self_, ctx, mensagem)

        assert ordem == ["atendeu"]  # apagar a mensagem apaga o anexo, que o Lavalink ainda vai baixar

    @pytest.mark.asyncio
    async def test_an_attachment_that_is_not_audio_is_deleted_in_the_strict_mode(self, monkeypatch):
        classe, self_, ctx, mensagem, _envios, _ = montar_pedido(monkeypatch, RuntimeError("não deve chegar aqui"))
        self_.bot.pool.servers.settings = AsyncMock(return_value={"request_channel_strict": True})
        self_.delete_message = AsyncMock()
        mensagem.content = ""
        mensagem.attachments = [SimpleNamespace(size=10, content_type="image/png", url="https://cdn.invalid/a.png")]
        self_.audio_formats = ("audio/mpeg",)

        await classe.song_requests(self_, ctx, mensagem)

        self_.delete_message.assert_awaited_once_with(mensagem)
        self_.parse_song_request.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_unreadable_configuration_is_logged_with_the_error_type_only(self, monkeypatch, caplog):
        from empiremuse.music_cog import song_requests as module

        quebrado = AsyncMock(side_effect=RuntimeError("segredo na mensagem"))
        mensagem = SimpleNamespace(guild=SimpleNamespace(id=1))
        self_ = SimpleNamespace(bot=SimpleNamespace(pool=SimpleNamespace(servers=SimpleNamespace(settings=quebrado))))

        with caplog.at_level("WARNING", logger=module.logger.name):
            assert await module._request_channel_strict(self_, mensagem) is False

        texto = "\n".join(r.getMessage() for r in caplog.records)
        assert "RuntimeError" in texto and "segredo" not in texto

    @pytest.mark.asyncio
    async def test_other_bots_and_commands_are_never_deleted(self, monkeypatch):
        assert "apagou" not in await pedir(monkeypatch, estrito=True, autor_bot=True)
        assert await pedir(monkeypatch, estrito=True, comando=object()) == []

    @pytest.mark.asyncio
    async def test_an_unavailable_store_leaves_the_channel_as_it_always_was(self, monkeypatch):
        quebrado = AsyncMock(side_effect=RuntimeError("store fora"))

        assert await pedir(monkeypatch, estrito=True, store=quebrado, mencoes=[object()]) == []
        assert await pedir(monkeypatch, estrito=True, store=quebrado) == ["atendeu"]

    @pytest.mark.asyncio
    async def test_deleting_a_message_that_is_already_gone_is_silent(self, caplog):
        mensagem = MagicMock()
        mensagem.channel.permissions_for.return_value.manage_messages = True
        mensagem.delete = AsyncMock(side_effect=disnake.NotFound(MagicMock(status=404), "gone"))
        self_ = SimpleNamespace(bot=SimpleNamespace(user=SimpleNamespace(id=1)))

        with caplog.at_level("ERROR"):
            await music.Music.delete_message(self_, mensagem)

        assert caplog.records == []


# ---------------------------------------------------------------------- painel apagado volta na hora


def mundo_do_painel(monkeypatch, *, mensagem_gravada=500):
    panel, pool = panel_world()
    pool.servers.request.get = AsyncMock(return_value=SimpleNamespace(message_id=mensagem_gravada, channel=7))
    pool.servers.request.peek_message_id = lambda gid: None  # documento fora do cache: lê de verdade
    now = [10_000.0]
    monkeypatch.setattr(panel_module.time, "monotonic", lambda: now[0])
    sleeps: list[float] = []

    async def sem_dormir(segundos):
        sleeps.append(segundos)

    monkeypatch.setattr(panel_module.asyncio, "sleep", sem_dormir)
    panel.handoff = AsyncMock()
    return panel, now, sleeps


async def esperar(panel):
    while panel._tasks:
        await asyncio.gather(*list(panel._tasks))


class TestPainelApagado:
    @pytest.mark.asyncio
    async def test_a_deletion_that_cannot_be_the_panel_never_copies_the_document(self, monkeypatch):
        panel, _, _ = mundo_do_painel(monkeypatch)
        panel.pool.servers.request.peek_message_id = lambda gid: 500  # em cache: o painel é a 500

        await panel.on_messages_deleted(GID, [1, 2, 3])
        await esperar(panel)

        panel.pool.servers.request.get.assert_not_awaited()
        assert panel.handoff.await_count == 0

    @pytest.mark.asyncio
    async def test_two_deletions_arriving_together_start_a_single_recovery(self, monkeypatch):
        panel, _, _ = mundo_do_painel(monkeypatch)

        await asyncio.gather(panel.on_messages_deleted(GID, [500]), panel.on_messages_deleted(GID, [500]))
        await esperar(panel)

        assert panel.handoff.await_count == 1

    @pytest.mark.asyncio
    async def test_deleting_the_panel_message_sends_a_new_panel_once(self, monkeypatch):
        panel, _, _ = mundo_do_painel(monkeypatch)

        await panel.on_messages_deleted(GID, [500])
        await panel.on_messages_deleted(GID, [500])  # o evento chega uma vez por bot
        await esperar(panel)

        assert panel.handoff.await_count == 1

    @pytest.mark.asyncio
    async def test_a_bulk_delete_that_includes_the_panel_counts(self, monkeypatch):
        panel, _, _ = mundo_do_painel(monkeypatch)

        await panel.on_messages_deleted(GID, [1, 2, 500, 3])
        await esperar(panel)

        assert panel.handoff.await_count == 1

    @pytest.mark.asyncio
    async def test_any_other_message_is_none_of_its_business(self, monkeypatch):
        panel, _, _ = mundo_do_painel(monkeypatch)

        await panel.on_messages_deleted(GID, [499, 501])
        await esperar(panel)

        assert panel.handoff.await_count == 0

    @pytest.mark.asyncio
    async def test_without_a_panel_message_stored_nothing_happens(self, monkeypatch):
        panel, _, _ = mundo_do_painel(monkeypatch, mensagem_gravada=None)

        await panel.on_messages_deleted(GID, [500])
        await esperar(panel)

        assert panel.handoff.await_count == 0

    @pytest.mark.asyncio
    async def test_someone_deleting_in_a_loop_gets_at_most_one_panel_per_window(self, monkeypatch):
        panel, now, sleeps = mundo_do_painel(monkeypatch)
        await panel.on_messages_deleted(GID, [500])
        await esperar(panel)
        now[0] += 1.0  # 1 s depois, apagam o painel novo

        await panel.on_messages_deleted(GID, [500])
        await esperar(panel)

        assert panel.handoff.await_count == 2
        assert sleeps == [4.0]  # a janela é de 5 s: o segundo painel espera os 4 s que faltam

    @pytest.mark.asyncio
    async def test_a_panel_already_replaced_by_another_path_is_not_sent_again(self, monkeypatch):
        panel, _, _ = mundo_do_painel(monkeypatch)
        panel.pool.servers.request.get = AsyncMock(
            side_effect=[SimpleNamespace(message_id=500, channel=7), SimpleNamespace(message_id=777, channel=7)]
        )

        await panel.on_messages_deleted(GID, [500])
        await esperar(panel)

        assert panel.handoff.await_count == 0

    @pytest.mark.asyncio
    async def test_a_failing_recovery_is_logged_and_the_guild_can_try_again(self, monkeypatch, caplog):
        panel, now, _ = mundo_do_painel(monkeypatch)
        panel.handoff = AsyncMock(side_effect=[RuntimeError("discord fora"), None])

        with caplog.at_level("WARNING", logger=panel_module.logger.name):
            await panel.on_messages_deleted(GID, [500])
            await esperar(panel)
        now[0] += 60
        await panel.on_messages_deleted(GID, [500])
        await esperar(panel)

        assert any("não recuperado" in r.getMessage() and "RuntimeError" in r.getMessage() for r in caplog.records)
        assert panel.handoff.await_count == 2


def painel_na_subida(monkeypatch, *, busca, eh_bot_do_painel=True):
    panel, pool = panel_world()
    bot = make_bot(1, "TOKEN", call=100)
    bot.guilds = [SimpleNamespace(id=GID)]
    pool.servers.request.get = AsyncMock(return_value=SimpleNamespace(message_id=500, channel=7))
    pool.servers.request.is_panel_bot = AsyncMock(return_value=eh_bot_do_painel)
    monkeypatch.setattr(panel_module, "fetch_panel_message", busca)
    panel.handoff = AsyncMock()
    return panel, bot


class TestConferenciaNaSubida:
    @pytest.mark.asyncio
    async def test_a_missing_panel_message_is_resent_when_this_bot_is_the_panel_bot(self, monkeypatch):
        gone = AsyncMock(side_effect=disnake.NotFound(MagicMock(status=404), "gone"))
        panel, bot = painel_na_subida(monkeypatch, busca=gone)

        await panel.verify_on_boot(bot)

        panel.handoff.assert_awaited_once_with(GID)

    @pytest.mark.asyncio
    async def test_a_forbidden_read_is_not_taken_for_a_missing_panel(self, monkeypatch, caplog):
        proibido = AsyncMock(side_effect=disnake.Forbidden(MagicMock(status=403), "sem histórico"))
        panel, bot = painel_na_subida(monkeypatch, busca=proibido)

        with caplog.at_level("WARNING", logger=panel_module.logger.name):
            await panel.verify_on_boot(bot)

        panel.handoff.assert_not_awaited()  # reenviar sem poder apagar o antigo deixaria um painel a mais a cada subida
        assert any("Forbidden" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_a_panel_message_of_the_bot_is_left_alone(self, monkeypatch):
        panel, bot = painel_na_subida(
            monkeypatch, busca=AsyncMock(return_value=SimpleNamespace(author=SimpleNamespace(id=1)))
        )

        await panel.verify_on_boot(bot)

        panel.handoff.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_bot_that_is_not_the_panel_bot_checks_nothing(self, monkeypatch):
        gone = AsyncMock(side_effect=disnake.NotFound(MagicMock(status=404), "gone"))
        panel, bot = painel_na_subida(monkeypatch, busca=gone, eh_bot_do_painel=False)

        await panel.verify_on_boot(bot)

        panel.handoff.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_panel_message_written_by_another_bot_is_replaced(self, monkeypatch):
        busca = AsyncMock(return_value=SimpleNamespace(author=SimpleNamespace(id=99)))
        panel, bot = painel_na_subida(monkeypatch, busca=busca)

        await panel.verify_on_boot(bot)

        panel.handoff.assert_awaited_once_with(GID)


# ---------------------------------------------------------------------- o painel: volume, progresso, fila e gênero


def montar(tr, entradas, **extra):
    bot = MagicMock()
    bot.get_color.return_value = 0
    bot.app_emojis = {}
    bot.pool.emoji_data = {}
    guilda = MagicMock()
    guilda.me.display_avatar.replace.return_value.url = "http://a"
    return render_panel(bot, guilda, entradas, tr=tr, **extra)


TOCANDO = PanelEntry("Bot A", 1, "Call A", False, "Faixa", "http://x", "playing", 2, None, 80, 45_000, 90_000)
PRIVADA = PanelEntry("Bot B", 2, "Call B", True, None, None, "playing", 0)


class TestPainelComVolumeEProgresso:
    def test_the_body_shows_the_progress_bar_and_the_volume(self):
        valor = montar(PT, [TOCANDO, PRIVADA])["embeds"][0].fields[0].value

        assert "▰▰▰▰▰▱▱▱▱▱ 0:45 / 1:30" in valor and "🔊 80%" in valor and "2 na fila" in valor

    def test_a_private_call_still_shows_nothing_of_the_track(self):
        valor = montar(PT, [TOCANDO, PRIVADA])["embeds"][0].fields[1].value

        assert valor == "-# 🔒 A call é privada: a faixa não aparece aqui." and "▰" not in valor and "🔊" not in valor

    def test_a_live_stream_has_no_bar(self):
        ao_vivo = TOCANDO._replace(duration_ms=None)

        assert "▰" not in montar(PT, [ao_vivo, PRIVADA])["embeds"][0].fields[0].value

    def test_the_last_action_goes_to_the_footer(self):
        rodape = montar(PT, [TOCANDO, PRIVADA], last_action="@Ana pausou")["embeds"][0].footer.text

        assert rodape.startswith("Última ação: @Ana pausou")


class TestFilaEGeneroNoPainel:
    def test_the_queue_button_is_in_its_own_row_and_works_in_both_languages(self):
        for tr, rotulo in ((PT, "Fila"), (EN, "Queue")):
            linha = montar(tr, [TOCANDO, PRIVADA])["components"][1]
            botao = linha.children[0]

            assert botao.custom_id == QUEUE_BUTTON_ID and botao.label == rotulo and len(linha.children) == 1

    def test_the_genre_select_only_exists_when_there_are_genres_and_uses_indexes(self):
        sem = montar(PT, [TOCANDO, PRIVADA])["components"]
        com = montar(PT, [TOCANDO, PRIVADA], genres=["Rock", "Pop"])["components"]

        assert all(getattr(c.children[0], "custom_id", "") != GENRE_SELECT_ID for c in sem)
        select = next(c.children[0] for c in com if getattr(c.children[0], "custom_id", "") == GENRE_SELECT_ID)
        assert [o.label for o in select.options] == ["Rock", "Pop"]
        assert [o.value for o in select.options] == [genre_key("Rock"), genre_key("Pop")]
        assert len(com) <= 5  # o Discord aceita no máximo 5 linhas

    def test_the_queue_select_options_carry_the_unique_id_and_the_choice_is_read_again_from_the_live_queue(self):
        faixa = SimpleNamespace(
            single_title="Canção",
            title="Canção - Banda",
            duration=61_000,
            is_stream=False,
            authors_string="Banda",
            unique_id="abc123",
        )
        opcoes = panel_interactions.queue_options([faixa])

        assert [o.value for o in opcoes] == ["abc123"] and opcoes[0].label.startswith("1. ")
        assert panel_interactions.queue_query_for([faixa], "abc123") == "Canção - Banda || ID > abc123"
        for ruim in ("zzz", "", None, 5):
            assert panel_interactions.queue_query_for([faixa], ruim) is None

    def test_a_queue_that_moved_while_the_view_was_open_still_picks_the_chosen_track(self):
        """Pela posição, a fila que andou mandaria para outra música; pelo ``unique_id`` a escolhida é a mesma, e se ela saiu, nada acontece."""
        a = SimpleNamespace(title="Hello", unique_id="id-a")
        b = SimpleNamespace(title="Hello (Live)", unique_id="id-b")
        c = SimpleNamespace(title="Outra", unique_id="id-c")

        assert panel_interactions.queue_query_for([a, b, c], "id-b").endswith(" || ID > id-b")
        assert panel_interactions.queue_query_for([c, b], "id-b").endswith(
            " || ID > id-b"
        )  # a faixa "a" saiu, "b" subiu
        assert panel_interactions.queue_query_for([c], "id-b") is None  # a escolhida já tocou ou saiu

    def test_a_title_that_contains_the_separator_cannot_break_the_request(self):
        esperta = SimpleNamespace(title="x || ID > y", unique_id="id-1")

        consulta = panel_interactions.queue_query_for([esperta], "id-1")

        assert consulta.count(" || ID > ") == 1 and consulta.endswith(" || ID > id-1")

    def test_a_genre_is_found_by_its_key_even_when_another_genre_appears_before_it(self):
        antes = ["Pop", "Rock"]
        chave = genre_key("Rock")
        depois = ["Axé", "Pop", "Rock"]  # um gênero novo entrou antes na ordem

        assert genre_for_key(antes, chave) == "Rock" and genre_for_key(depois, chave) == "Rock"
        assert genre_for_key(["Pop"], chave) is None  # o gênero sumiu: recusa, nunca troca por outro
        assert genre_for_key(depois, 3) is None and genre_for_key(depois, None) is None

    def test_genre_keys_fit_a_select_value_and_ignore_case(self):
        assert len(genre_key("a" * 500)) <= 100 and genre_key("ROCK") == genre_key("rock")

    @pytest.mark.asyncio
    async def test_a_long_queue_pages_at_25_options_per_select(self):
        faixas = [
            SimpleNamespace(
                single_title="T", title="T", duration=1000, is_stream=False, authors_string="A", unique_id=f"id-{n}"
            )
            for n in range(60)
        ]
        opcoes = panel_interactions.queue_options(faixas)

        visao = SelectInteraction(user=SimpleNamespace(id=1), opts=opcoes, timeout=1)

        assert [len(p) for p in visao.item_pages] == [25, 25, 10]


# ---------------------------------------------------------------------- toda resposta a clique no painel é efêmera


def test_every_reply_to_a_panel_interaction_is_ephemeral():
    """Quem clica no painel compartilhado nunca deixa a resposta no canal: ``send`` sempre com ``ephemeral``."""
    raiz = pathlib.Path(music.__file__).resolve().parent.parent
    arquivos = sorted((raiz / "music_cog").glob("controller_*.py")) + [
        raiz / "music_cog" / "player_controller.py",
        raiz / "music_cog" / "panel_interactions.py",
        raiz / "utils" / "music" / "interactions" / "request_panel.py",
    ]
    soltos = []
    for arquivo in arquivos:
        for no in ast.walk(ast.parse(arquivo.read_text(encoding="utf-8"))):
            if (
                isinstance(no, ast.Call)
                and isinstance(no.func, ast.Attribute)
                and no.func.attr in ("send", "send_message")
                and "interaction" in ast.unparse(no.func.value)
                and "ephemeral" not in {k.arg for k in no.keywords}
            ):
                soltos.append(f"{arquivo.name}:{no.lineno}")

    assert soltos == []


# ---------------------------------------------------------------------- os cliques do painel respondem em mensagem efêmera própria


def clique(*, valores=None, custom_id=QUEUE_BUTTON_ID, em_call=True):
    resposta = SimpleNamespace(defer=AsyncMock(), edit_message=AsyncMock(), is_done=lambda: False)
    interacao = SimpleNamespace(
        data=SimpleNamespace(custom_id=custom_id, values=valores or []),
        response=resposta,
        edit_original_message=AsyncMock(),
        guild=SimpleNamespace(id=GID),
        guild_id=GID,
        author=SimpleNamespace(id=7),
        user=SimpleNamespace(id=7, voice=SimpleNamespace(channel=SimpleNamespace(id=100)) if em_call else None),
    )
    pool = SimpleNamespace(i18n=SimpleNamespace(for_guild=AsyncMock(return_value=PT)), request_panel=None)
    eu = SimpleNamespace(bot=SimpleNamespace(pool=pool, dispatch=MagicMock()))
    return panel_interactions.PanelInteractionsMixin, eu, interacao


class TestCliquesDoPainel:
    @pytest.mark.asyncio
    async def test_the_queue_button_opens_its_own_ephemeral_message_and_never_edits_the_panel(self, monkeypatch):
        monkeypatch.setattr(panel_interactions, "resolve_player", lambda *a: None)
        classe, eu, interacao = clique()

        await classe.request_panel_queue(eu, interacao)

        interacao.response.defer.assert_awaited_once_with(ephemeral=True, with_message=True)
        interacao.edit_original_message.assert_awaited_once()
        assert (
            "Entre" in interacao.edit_original_message.await_args.kwargs["content"]
            or "call" in (interacao.edit_original_message.await_args.kwargs["content"])
        )

    @pytest.mark.asyncio
    async def test_the_queue_button_of_somebody_elses_component_is_ignored(self):
        classe, eu, interacao = clique(custom_id="outro")

        await classe.request_panel_queue(eu, interacao)

        interacao.response.defer.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_an_empty_queue_says_so_privately(self, monkeypatch):
        player = SimpleNamespace(queue=[])
        monkeypatch.setattr(panel_interactions, "resolve_player", lambda *a: (MagicMock(), player))
        classe, eu, interacao = clique()

        await classe.request_panel_queue(eu, interacao)

        assert "vazia" in interacao.edit_original_message.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_the_genre_select_with_nothing_chosen_only_acknowledges_the_panel_update(self):
        classe, eu, interacao = clique(custom_id=GENRE_SELECT_ID, valores=[])

        await classe.request_panel_genre(eu, interacao)

        interacao.response.defer.assert_awaited_once_with()

    @pytest.mark.asyncio
    async def test_the_genre_select_opens_an_ephemeral_message_and_asks_to_join_a_call(self):
        classe, eu, interacao = clique(custom_id=GENRE_SELECT_ID, valores=["0"], em_call=False)

        await classe.request_panel_genre(eu, interacao)

        interacao.response.defer.assert_awaited_once_with(ephemeral=True, with_message=True)
        assert "canal de voz" in interacao.edit_original_message.await_args.kwargs["content"]


class TestFimDoFluxoDoPainel:
    @pytest.mark.asyncio
    async def test_a_click_inside_the_view_is_answered_on_that_interaction(self):
        clicada = SimpleNamespace(response=SimpleNamespace(is_done=lambda: False, edit_message=AsyncMock()))
        original = SimpleNamespace(edit_original_message=AsyncMock())

        await panel_interactions._say(SimpleNamespace(inter=clicada), original, content="x", view=None)

        clicada.response.edit_message.assert_awaited_once_with(content="x", view=None)
        original.edit_original_message.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_a_timeout_edits_the_original_ephemeral_message(self):
        original = SimpleNamespace(edit_original_message=AsyncMock())

        await panel_interactions._say(SimpleNamespace(inter=None), original, content="x", view=None)

        original.edit_original_message.assert_awaited_once_with(content="x", view=None)


class FakeView:
    """Uma ``SelectInteraction`` que já nasce respondida: ``selected``/``inter`` vêm do teste, e ``wait`` volta na hora."""

    escolha: object = None
    clique: object = None

    def __init__(self, *args, **kwargs):
        self.selected = type(self).escolha
        self.inter = type(self).clique

    async def wait(self):
        return True


def faixa_com_id(unique_id, title="Canção"):
    return SimpleNamespace(
        single_title=title, title=title, duration=1000, is_stream=False, authors_string="A", unique_id=unique_id
    )


class TestFilaDePontaAPonta:
    @staticmethod
    def preparar(monkeypatch, *, fila, escolha, clicou=True):
        pulou = AsyncMock()
        bot_da_call = SimpleNamespace(
            get_guild=lambda gid: SimpleNamespace(id=gid),
            get_cog=lambda nome: SimpleNamespace(process_player_interaction=pulou),
            get_slash_command=lambda nome: f"/{nome}",
        )
        player = SimpleNamespace(queue=fila)
        monkeypatch.setattr(panel_interactions, "resolve_player", lambda *a: (bot_da_call, player))
        monkeypatch.setattr(panel_interactions, "SelectInteraction", FakeView)
        FakeView.escolha = escolha
        FakeView.clique = (
            SimpleNamespace(response=SimpleNamespace(is_done=lambda: False, edit_message=AsyncMock()))
            if clicou
            else None
        )
        return pulou, player

    @pytest.mark.asyncio
    async def test_the_chosen_track_is_skipped_to_by_its_unique_id(self, monkeypatch):
        pulou, _ = self.preparar(monkeypatch, fila=[faixa_com_id("a"), faixa_com_id("b", "Hello")], escolha="b")
        classe, eu, interacao = clique()

        await classe.request_panel_queue(eu, interacao)

        pulou.assert_awaited_once()
        assert pulou.await_args.kwargs["kwargs"] == {"query": "Hello || ID > b", "case_sensitive": True}
        assert pulou.await_args.kwargs["command"] == "/skipto"
        eu.bot.dispatch.assert_not_called()  # nenhum erro engolido

    @pytest.mark.asyncio
    async def test_cancel_answers_the_clicked_button_and_skips_nothing(self, monkeypatch):
        pulou, _ = self.preparar(monkeypatch, fila=[faixa_com_id("a")], escolha=False)
        classe, eu, interacao = clique()

        await classe.request_panel_queue(eu, interacao)

        pulou.assert_not_awaited()
        FakeView.clique.response.edit_message.assert_awaited_once()
        eu.bot.dispatch.assert_not_called()

    @pytest.mark.asyncio
    async def test_a_track_that_left_the_queue_while_the_view_was_open_is_not_replaced_by_another(self, monkeypatch):
        pulou, player = self.preparar(monkeypatch, fila=[faixa_com_id("a")], escolha="b")
        classe, eu, interacao = clique()

        await classe.request_panel_queue(eu, interacao)

        pulou.assert_not_awaited()
        FakeView.clique.response.edit_message.assert_awaited_once()
        assert "mudou" in FakeView.clique.response.edit_message.await_args.kwargs["content"]

    @pytest.mark.asyncio
    async def test_a_view_that_timed_out_edits_the_original_message(self, monkeypatch):
        pulou, _ = self.preparar(monkeypatch, fila=[faixa_com_id("a")], escolha=None, clicou=False)
        classe, eu, interacao = clique()

        await classe.request_panel_queue(eu, interacao)

        pulou.assert_not_awaited()
        assert interacao.edit_original_message.await_count == 2  # o prompt com a view e o aviso de cancelado


class TestGeneroContraTodosOsGeneros:
    @pytest.mark.asyncio
    async def test_a_genre_beyond_the_rendered_page_is_still_found_by_its_key(self, monkeypatch):
        """O painel mostra só os primeiros 25 gêneros; uma lista que mudou pode ter empurrado o escolhido para além deles."""
        nomes = [f"Gênero {n:02}" for n in range(30)]
        playlists = tuple(SimpleNamespace(name=f"P{n}", genres=(nome,)) for n, nome in enumerate(nomes))
        escolhido = nomes[27]  # na ordem de agora ele é o 28º: fora dos 25 desenhados
        vistos = []

        class VistaFalsa(FakeView):
            def __init__(self, user, bot, chosen, **kwargs):
                super().__init__()
                vistos.append([p.name for p in chosen])

        async def lista(bot, gid):
            return playlists

        monkeypatch.setattr(panel_interactions, "safe_list", lista)
        monkeypatch.setattr(panel_interactions, "GuildPlaylistSelect", VistaFalsa)
        FakeView.escolha, FakeView.clique = None, None
        classe, eu, interacao = clique(custom_id=GENRE_SELECT_ID, valores=[genre_key(escolhido)])

        await classe.request_panel_genre(eu, interacao)

        assert vistos == [["P27"]]  # abriu as playlists daquele gênero, não a mensagem de "não há mais"

    @pytest.mark.asyncio
    async def test_a_key_of_a_genre_that_is_gone_is_refused(self, monkeypatch):
        async def lista(bot, gid):
            return (SimpleNamespace(name="P", genres=("Rock",)),)

        monkeypatch.setattr(panel_interactions, "safe_list", lista)
        classe, eu, interacao = clique(custom_id=GENRE_SELECT_ID, valores=[genre_key("Jazz")])

        await classe.request_panel_genre(eu, interacao)

        assert "mais" in interacao.edit_original_message.await_args.kwargs["content"]
