"""E5, portão do banimento por canal (contrato §7): interações pelo parser REAL do disnake (R8.1), ``on_message`` (R8.2), isenção (R8.6),
comandos do dono (R8.5) e scrobble (R8.10). Discord, banco e relógio são falsos; o parser de ``INTERACTION_CREATE`` é o do disnake instalado.
"""

import asyncio
import types
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import disnake
import pytest

from empiremuse.cogs.lastfm import LastFmCog
from empiremuse.cogs.user_bans import UserBansCog
from empiremuse.i18n import get_i18n
from empiremuse.rules.user_ban import UserBanRecord, WarningLimiter
from empiremuse.utils import user_ban_views as views
from empiremuse.utils.client import BotCore
from empiremuse.utils.user_ban import UserBans
from empiremuse.utils.user_ban_gate import install_interaction_gate

pytestmark = pytest.mark.critical

DONO, BANIDO, COMUM, DONO_DA_APP, BOT_ID = (
    100000000000000009,
    100000000000000001,
    100000000000000002,
    100000000000000008,
    90000000000000001,
)
MOTIVO = "MOTIVO-SECRETO-DO-DONO"
PT = get_i18n().for_locale("pt-BR")
EN = get_i18n().for_locale("en-US")


RELOGIO = SimpleNamespace(t=0.0)  # relógio da cota de avisos (Q13), zerado a cada lista


def banidos(*ids, owners=(DONO,)):
    RELOGIO.t = 0.0
    bans = UserBans(SimpleNamespace(), owners=frozenset(owners))
    bans._warnings = WarningLimiter(clock=lambda: RELOGIO.t)
    bans._records = {uid: UserBanRecord(uid, 1, MOTIVO, DONO) for uid in ids}
    bans._loaded = True
    return bans


def usuario(user_id):
    return {"id": str(user_id), "username": "u", "discriminator": "0", "avatar": None, "global_name": None}


MENSAGEM = {
    "id": "9",
    "channel_id": "3",
    "type": 0,
    "content": "x",
    "attachments": [],
    "embeds": [],
    "pinned": False,
    "mention_everyone": False,
    "tts": False,
    "timestamp": "2026-01-01T00:00:00+00:00",
    "edited_timestamp": None,
    "mentions": [],
    "mention_roles": [],
    "flags": 0,
    "components": [],
}
DATA = {
    2: {"id": "5", "name": "play", "type": 1, "options": []},
    3: {"custom_id": "abc:1", "component_type": 2},
    4: {
        "id": "5",
        "name": "play",
        "type": 1,
        "options": [{"name": "query", "type": 3, "value": "a", "focused": True}],
    },
    5: {"custom_id": "abc:1", "components": []},
}


def payload(tipo, user_id, *, em_dm=True, locale="pt-BR", **data):
    """JSON de ``INTERACTION_CREATE`` completo o bastante para o parser real: DM (``user``) ou servidor (``member.user``)."""
    corpo = {
        "id": "1",
        "application_id": "2",
        "token": "t",
        "version": 1,
        "type": tipo,
        "channel_id": "3",
        "channel": {"id": "3", "type": 1, "last_message_id": None, "recipients": [usuario(user_id)]},
        "locale": locale,
        "app_permissions": "0",
        "authorizing_integration_owners": {},
        "entitlements": [],
        "attachment_size_limit": 10485760,
        "data": {**DATA[tipo], **data},
    }
    if tipo == 3:
        corpo["message"] = {**MENSAGEM, "author": usuario(user_id)}
    if em_dm:
        corpo["user"] = usuario(user_id)
    else:
        corpo["guild_id"] = "7"
        corpo["member"] = {
            "user": usuario(user_id),
            "roles": [],
            "joined_at": "2026-01-01T00:00:00+00:00",
            "deaf": False,
            "mute": False,
            "flags": 0,
            "permissions": "0",
        }
    return corpo


def novo_bot(bans, *, instalar=True):
    bot = disnake.Client()
    bot.pool = SimpleNamespace(i18n=get_i18n())
    state = bot._connection
    spies = SimpleNamespace(
        eventos=MagicMock(),
        views=MagicMock(wraps=state._view_store.dispatch),
        modais=MagicMock(wraps=state._modal_store.dispatch),
    )
    state.dispatch = spies.eventos
    state._view_store.dispatch = spies.views
    state._modal_store.dispatch = spies.modais
    bot.http.create_interaction_response = AsyncMock()
    gate = install_interaction_gate(bot, bans) if instalar else None
    return SimpleNamespace(
        bot=bot, state=state, spies=spies, gate=gate, enviar=lambda p: state.parsers["INTERACTION_CREATE"](p)
    )


async def assentar():
    for _ in range(3):
        await asyncio.sleep(0)


def chegou_ao_callback(bot, tipo):
    """O parser real do disnake foi chamado? (evento ``interaction`` e, nos tipos 3 e 5, o armazém de views ou de modais)."""
    nomes = [chamada.args[0] for chamada in bot.spies.eventos.call_args_list]
    extra = {3: bot.spies.views, 5: bot.spies.modais}.get(tipo)
    return "interaction" in nomes and (extra is None or extra.called)


def nada_chegou(bot):
    return not bot.spies.eventos.called and not bot.spies.views.called and not bot.spies.modais.called


# ------------------------------------------------------------------------------- R8.1: interações


@pytest.mark.asyncio
@pytest.mark.parametrize("papel", ["principal", "reserva"])
@pytest.mark.parametrize("em_dm", [True, False], ids=["dm", "servidor"])
@pytest.mark.parametrize("tipo", [2, 3, 4, 5])
async def test_a_banned_user_never_reaches_a_callback_and_gets_a_short_ephemeral_reply(tipo, em_dm, papel):
    bot = novo_bot(banidos(BANIDO))

    bot.enviar(payload(tipo, BANIDO, em_dm=em_dm))
    await assentar()

    assert nada_chegou(bot), f"{papel}: o callback do tipo {tipo} não pode rodar para banido"
    chamada = bot.bot.http.create_interaction_response.await_args
    assert chamada.args == ("1", "t")
    if tipo == 4:
        assert chamada.kwargs == {"type": 8, "data": {"choices": []}}  # autocompletar: lista vazia
        return
    assert chamada.kwargs["type"] == 4 and chamada.kwargs["data"]["flags"] == 64  # efêmera
    texto = chamada.kwargs["data"]["content"]
    assert texto == PT("erasure.ban.blocked") and MOTIVO not in texto and str(BANIDO) not in texto  # D4: sem motivo


@pytest.mark.asyncio
@pytest.mark.parametrize("em_dm", [True, False], ids=["dm", "servidor"])
@pytest.mark.parametrize("tipo", [2, 3, 4, 5])
@pytest.mark.parametrize("quem", [COMUM, DONO, DONO_DA_APP])
async def test_common_users_and_owners_pass_through_the_real_parser(tipo, quem, em_dm):
    bot = novo_bot(banidos(BANIDO))

    bot.enviar(payload(tipo, quem, em_dm=em_dm))  # em servidor o ID vem de ``member.user.id``
    await assentar()

    assert chegou_ao_callback(bot, tipo)
    bot.bot.http.create_interaction_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_reply_follows_the_interaction_locale():
    bot = novo_bot(banidos(BANIDO))

    bot.enviar(payload(2, BANIDO, locale="en-US"))
    await assentar()

    assert bot.bot.http.create_interaction_response.await_args.kwargs["data"]["content"] == EN("erasure.ban.blocked")


@pytest.mark.asyncio
@pytest.mark.parametrize("remover", ["user", "member"])
async def test_an_interaction_without_a_valid_author_is_denied(remover):
    bot = novo_bot(banidos(BANIDO))
    corpo = payload(2, COMUM, em_dm=remover == "member")
    corpo.pop("user", None)
    corpo.pop("member", None)

    bot.enviar(corpo)
    await assentar()

    assert nada_chegou(bot)  # ID inválido nega


@pytest.mark.asyncio
async def test_a_ping_and_unknown_types_are_left_to_the_original_parser():
    bot = novo_bot(banidos(BANIDO))

    bot.enviar({"type": 1, "id": "1", "token": "t"})
    bot.enviar({"type": 99, "id": "1", "token": "t"})
    await assentar()

    bot.bot.http.create_interaction_response.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_failing_reply_is_swallowed_and_the_callback_stays_blocked():
    bot = novo_bot(banidos(BANIDO))
    bot.bot.http.create_interaction_response.side_effect = disnake.HTTPException(MagicMock(status=500), "x")

    bot.enviar(payload(2, BANIDO))
    await assentar()

    assert nada_chegou(bot)


@pytest.mark.asyncio
async def test_the_gate_fails_the_boot_when_the_parser_is_missing():
    bot = novo_bot(banidos(), instalar=False)
    del bot.state.parsers["INTERACTION_CREATE"]

    with pytest.raises(RuntimeError, match="INTERACTION_CREATE"):
        install_interaction_gate(bot.bot, banidos())
    with pytest.raises(RuntimeError):
        install_interaction_gate(SimpleNamespace(), banidos())


@pytest.mark.asyncio
async def test_a_list_not_loaded_blocks_no_valid_user():
    bans = UserBans(SimpleNamespace(), owners=frozenset({DONO}))  # D2: sem carga, o bot abre
    bot = novo_bot(bans)

    bot.enviar(payload(2, BANIDO))
    await assentar()

    assert chegou_ao_callback(bot, 2)


# ------------------------------------------------------------------------------- R8.6: isenção


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tipo", "data"),
    [
        pytest.param(2, {"name": "apagar_meus_dados"}, id="slash pt-BR"),
        pytest.param(2, {"name": "delete_my_data"}, id="slash en-US"),
        pytest.param(3, {"custom_id": "erasure_confirm:abc"}, id="confirmar"),
        pytest.param(3, {"custom_id": "erasure_cancel:abc"}, id="cancelar"),
        pytest.param(3, {"custom_id": "erasure_grant:abc"}, id="concessão"),
        pytest.param(3, {"custom_id": "erasure_grant_cancel:abc"}, id="cancelar concessão"),
    ],
)
async def test_the_banned_user_keeps_exactly_the_erasure_self_service(tipo, data):
    bot = novo_bot(banidos(BANIDO))

    bot.enviar(payload(tipo, BANIDO, **data))
    await assentar()

    assert chegou_ao_callback(bot, tipo)
    bot.bot.http.create_interaction_response.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tipo", "data"),
    [
        pytest.param(3, {"custom_id": "erasure_confirm_x:abc"}, id="prefixo parecido"),
        pytest.param(3, {"custom_id": "erasure_confirmx"}, id="sem dois-pontos"),
        pytest.param(3, {"custom_id": "erasure_owner_send:abc"}, id="pedido do dono"),
        pytest.param(2, {"name": "apagar_meus_dados_x"}, id="nome parecido"),
        pytest.param(2, {"name": "apagar_meus_dado"}, id="nome truncado"),
        pytest.param(5, {"custom_id": "erasure_confirm:abc"}, id="modal com id de apagar"),
        pytest.param(4, {"name": "apagar_meus_dados"}, id="autocompletar"),
    ],
)
async def test_similar_names_and_ids_stay_blocked(tipo, data):
    bot = novo_bot(banidos(BANIDO))

    bot.enviar(payload(tipo, BANIDO, **data))
    await assentar()

    assert nada_chegou(bot)


# ------------------------------------------------------------------------------- R8.2: mensagem


class Chegou(Exception):
    """A mensagem passou do portão e chegou ao ``get_context`` (o resto de ``on_message`` não importa aqui)."""


def bot_de_mensagens(bans, *, reserva=False):
    relogio = RELOGIO
    self = SimpleNamespace(
        bot_ready=True,
        appinfo=object(),
        is_closed=lambda: False,
        is_reserve=reserva,
        pool=SimpleNamespace(user_bans=bans, i18n=get_i18n()),
        user=SimpleNamespace(id=BOT_ID, mention=f"<@{BOT_ID}>"),
        music=SimpleNamespace(players={}),
        config={"ENABLE_SONGREQUEST_MENTION": True},
        get_prefix=AsyncMock(return_value="!!"),
        can_send_message=AsyncMock(return_value=True),
        get_context=AsyncMock(side_effect=Chegou),
        dispatch=MagicMock(),
        _is_radio_only=lambda: False,
    )
    self._refuse_blocked_message = types.MethodType(BotCore._refuse_blocked_message, self)
    self._is_addressed_to_bot = types.MethodType(BotCore._is_addressed_to_bot, self)
    self.relogio = relogio
    return self


def mensagem(autor, texto, *, canal=None):
    return SimpleNamespace(
        author=SimpleNamespace(id=autor, bot=False, voice=object()),
        content=texto,
        guild=SimpleNamespace(id=7, preferred_locale=disnake.Locale.pt_BR, me=object()),
        channel=canal or MagicMock(spec=disnake.TextChannel),
        flags=SimpleNamespace(ephemeral=False),
        id=11,
        reply=AsyncMock(),
        mentions=[],
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("reserva", [False, True], ids=["principal", "reserva"])
@pytest.mark.parametrize(
    "texto",
    [
        pytest.param("!!play musica", id="prefixo"),
        pytest.param(f"<@{BOT_ID}>", id="menção"),
        pytest.param(f"<@!{BOT_ID}>", id="menção com !"),
        pytest.param(f"<@{BOT_ID}> toca uma musica", id="pedido por menção"),
    ],
)
async def test_a_banned_message_is_dropped_before_any_processing_and_answered_once_per_hour(texto, reserva):
    bot = bot_de_mensagens(banidos(BANIDO), reserva=reserva)
    primeira, segunda = mensagem(BANIDO, texto), mensagem(BANIDO, texto)

    await BotCore.on_message(bot, primeira)
    await BotCore.on_message(bot, segunda)

    bot.get_context.assert_not_awaited()
    bot.dispatch.assert_not_called()  # nem ``jukebox_message`` nem pedido por menção
    primeira.reply.assert_awaited_once()
    assert primeira.reply.await_args.args == (PT("erasure.ban.blocked_message"),)
    assert primeira.reply.await_args.kwargs["allowed_mentions"].users is False
    assert MOTIVO not in str(primeira.reply.await_args) and "banimento" not in primeira.reply.await_args.args[0].lower()
    segunda.reply.assert_not_awaited()  # Q13: 1 por usuário por hora
    bot.relogio.t = 3600.0
    terceira = mensagem(BANIDO, texto)
    await BotCore.on_message(bot, terceira)
    terceira.reply.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "canal",
    [
        pytest.param(MagicMock(spec=disnake.StageChannel), id="canal de palco"),
        pytest.param(MagicMock(spec=disnake.TextChannel), id="canal jukebox ou comum"),
    ],
)
async def test_plain_chat_from_a_banned_user_gets_no_warning_and_no_processing(canal):
    bot = bot_de_mensagens(banidos(BANIDO))
    conversa = mensagem(BANIDO, "bom dia pessoal", canal=canal)

    await BotCore.on_message(bot, conversa)

    conversa.reply.assert_not_awaited()
    bot.get_context.assert_not_awaited()
    bot.dispatch.assert_not_called()


@pytest.mark.asyncio
async def test_a_stage_channel_message_from_a_banned_user_does_not_fall_through_the_chain():
    bot = bot_de_mensagens(banidos(BANIDO))
    palco = mensagem(BANIDO, "!!play x", canal=MagicMock(spec=disnake.StageChannel))

    await BotCore.on_message(bot, palco)

    bot.get_context.assert_not_awaited()  # o ``pass`` do canal de palco não pode deixar escapar


@pytest.mark.asyncio
@pytest.mark.parametrize("autor", [COMUM, DONO, DONO_DA_APP])
async def test_everyone_else_reaches_the_context(autor):
    bot = bot_de_mensagens(banidos(BANIDO))
    msg = mensagem(autor, "!!play x")

    with pytest.raises(Chegou):
        await BotCore.on_message(bot, msg)
    msg.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_warning_needs_permission_to_send_and_survives_a_discord_error():
    bot = bot_de_mensagens(banidos(BANIDO))
    msg = mensagem(BANIDO, "!!play x")
    bot.can_send_message.return_value = False
    await BotCore.on_message(bot, msg)
    msg.reply.assert_not_awaited()

    bot.can_send_message.return_value = True
    bot.relogio.t = 7200.0
    outra = mensagem(BANIDO, "!!play x")
    outra.reply.side_effect = disnake.HTTPException(MagicMock(status=403), "x")
    await BotCore.on_message(bot, outra)  # não levanta
    bot.get_context.assert_not_awaited()


@pytest.mark.asyncio
async def test_without_a_readable_prefix_there_is_no_warning():
    bot = bot_de_mensagens(banidos(BANIDO))
    bot.get_prefix.side_effect = RuntimeError("banco fora")
    msg = mensagem(BANIDO, "!!play x")

    await BotCore.on_message(bot, msg)

    msg.reply.assert_not_awaited()


# ------------------------------------------------------------------------------- R8.5: dono


class BancoSimples:
    def __init__(self):
        self.docs = {}

    async def query_data(self, **_):
        return [{"_id": str(k), **v} for k, v in self.docs.items()]

    async def update_data(self, id_, data, **_):
        self.docs[id_] = data

    async def delete_data(self, id_, *_):
        self.docs.pop(id_, None)


def mundo_do_dono(*, reserva=False):
    pool_stub = SimpleNamespace(database=BancoSimples(), panel_sessions=None)
    bans = UserBans(pool_stub, owners=frozenset({DONO}), rpc_closer=lambda uid, motivo: True)
    alvo = SimpleNamespace(
        id=COMUM, bot=False, display_name="Fulano", display_avatar=SimpleNamespace(url="https://x/a.png")
    )
    bot = SimpleNamespace(
        pool=SimpleNamespace(
            i18n=get_i18n(), user_bans=bans, get_all_bots=lambda: [SimpleNamespace(user=SimpleNamespace(id=BOT_ID))]
        ),
        env_owner_ids={DONO},
        default_prefix="!!",
        is_reserve=reserva,
        is_owner=AsyncMock(return_value=True),  # o dono da aplicação: NUNCA basta
        fetch_user=AsyncMock(return_value=alvo),
    )
    return SimpleNamespace(bot=bot, bans=bans, cog=UserBansCog(bot), alvo=alvo)


def dm(autor, texto):
    canal = MagicMock(spec=disnake.DMChannel)
    canal.send = AsyncMock(return_value=SimpleNamespace(edit=AsyncMock()))
    return SimpleNamespace(author=SimpleNamespace(id=autor), content=texto, channel=canal)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("autor", "reserva"),
    [
        pytest.param(DONO_DA_APP, False, id="dono da aplicação fora de OWNER_IDS"),
        pytest.param(COMUM, False, id="comum"),
        pytest.param(DONO, True, id="bot reserva"),
    ],
)
async def test_the_dm_command_ignores_everyone_but_env_owners_on_the_main_bot(autor, reserva):
    w = mundo_do_dono(reserva=reserva)
    msg = dm(autor, f"!!banuser {COMUM} abuso")

    await w.cog.owner_ban_dm(msg)

    msg.channel.send.assert_not_awaited()
    w.bot.is_owner.assert_not_awaited()  # nunca ``is_owner``
    w.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
async def test_the_dm_command_outside_a_dm_is_ignored():
    w = mundo_do_dono()
    msg = dm(DONO, f"!!banuser {COMUM}")
    msg.channel = MagicMock(spec=disnake.TextChannel)

    await w.cog.owner_ban_dm(msg)

    w.bot.fetch_user.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("argumento", "chave"),
    [
        pytest.param("abc", "erasure.owner.invalid_id", id="ID inválido"),
        pytest.param("123", "erasure.owner.invalid_id", id="curto demais"),
        pytest.param(str(DONO), "erasure.ban.owner.target_owner", id="alvo dono"),
        pytest.param(str(BOT_ID), "erasure.ban.owner.target_bot", id="alvo bot do pool"),
    ],
)
async def test_the_dm_command_refuses_invalid_owner_and_bot_targets(argumento, chave):
    w = mundo_do_dono()
    msg = dm(DONO, f"!!banuser {argumento} motivo")

    await w.cog.owner_ban_dm(msg)

    assert msg.channel.send.await_args.kwargs["content"] == PT(chave)
    assert msg.channel.send.await_args.kwargs["view"] is None
    w.bot.fetch_user.assert_not_awaited()
    assert not w.bans.is_blocked(COMUM)


@pytest.mark.asyncio
async def test_a_bot_user_resolved_by_discord_is_refused():
    w = mundo_do_dono()
    w.alvo.bot = True
    msg = dm(DONO, f"!!banuser {COMUM}")

    await w.cog.owner_ban_dm(msg)

    assert msg.channel.send.await_args.kwargs["content"] == PT("erasure.ban.owner.target_bot")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("erro", "chave"),
    [
        (disnake.NotFound(MagicMock(status=404), "x"), "erasure.owner.not_found"),
        (disnake.HTTPException(MagicMock(status=500), "x"), "erasure.owner.lookup_failed"),
    ],
)
async def test_an_unresolvable_user_is_refused(erro, chave):
    w = mundo_do_dono()
    w.bot.fetch_user.side_effect = erro
    msg = dm(DONO, f"!!banuser {COMUM}")

    await w.cog.owner_ban_dm(msg)

    assert msg.channel.send.await_args.kwargs["content"] == PT(chave)


def clique(autor, locale=disnake.Locale.pt_BR):
    return SimpleNamespace(
        author=SimpleNamespace(id=autor),
        locale=locale,
        response=SimpleNamespace(
            send_message=AsyncMock(), edit_message=AsyncMock(), defer=AsyncMock(), send_modal=AsyncMock()
        ),
        followup=SimpleNamespace(send=AsyncMock()),
        edit_original_message=AsyncMock(),
    )


def interacao_de_mensagem(autor):
    """``isinstance(..., disnake.MessageInteraction)`` verdadeiro, sem construir uma interação real."""
    base = clique(autor)
    inter = MagicMock(spec=disnake.MessageInteraction)
    inter.author, inter.locale, inter.response, inter.followup = base.author, base.locale, base.response, base.followup
    return inter


@pytest.mark.asyncio
async def test_the_owner_sees_the_resolved_user_and_the_confirmation_bans_and_audits_without_the_reason(caplog):
    w = mundo_do_dono()
    msg = dm(DONO, f"!!banuser {COMUM} {MOTIVO}")

    await w.cog.owner_ban_dm(msg)

    enviado = msg.channel.send.await_args.kwargs
    assert enviado["allowed_mentions"].users is False and "Fulano" in enviado["embed"].description
    assert MOTIVO in enviado["embed"].description  # só o dono vê o motivo (escapado)
    view = enviado["view"]
    assert isinstance(view, views.UserBanConfirmView) and not w.bans.is_blocked(COMUM)  # ainda não baniu

    assert await view.interaction_check(clique(DONO_DA_APP)) is False  # outro dono da aplicação
    assert await view.interaction_check(clique(COMUM)) is False
    assert not w.bans.is_blocked(COMUM)
    assert await view.interaction_check(clique(DONO)) is True

    caplog.set_level("WARNING")
    ok = clique(DONO)
    await view.confirmed(ok)

    assert w.bans.is_blocked(COMUM)
    assert ok.edit_original_message.await_args.kwargs["content"] == PT(
        "erasure.ban.owner.banned", name="Fulano", id=COMUM
    )
    assert MOTIVO not in caplog.text  # R8.9: o log não leva o motivo


@pytest.mark.asyncio
async def test_a_second_ban_confirmation_says_already_banned_and_unban_works():
    w = mundo_do_dono()
    await w.bans.ban(DONO, COMUM, "")
    msg = dm(DONO, f"!!banuser {COMUM}")
    await w.cog.owner_ban_dm(msg)
    repetido = clique(DONO)
    await msg.channel.send.await_args.kwargs["view"].confirmed(repetido)
    assert repetido.edit_original_message.await_args.kwargs["content"] == PT("erasure.ban.owner.already")

    msg = dm(DONO, f"!!unbanuser {COMUM}")
    await w.cog.owner_ban_dm(msg)
    liberar = clique(DONO)
    await msg.channel.send.await_args.kwargs["view"].confirmed(liberar)
    assert not w.bans.is_blocked(COMUM)
    assert liberar.edit_original_message.await_args.kwargs["content"] == PT(
        "erasure.ban.owner.unbanned", name="Fulano", id=COMUM
    )
    assert (await w.bans.unban(DONO, COMUM)) is False


@pytest.mark.asyncio
async def test_a_database_error_in_the_confirmation_is_a_fixed_text_and_nothing_is_blocked():
    w = mundo_do_dono()
    w.bans.pool.database.update_data = AsyncMock(side_effect=ConnectionError("senha=SEGREDO"))
    msg = dm(DONO, f"!!banuser {COMUM}")
    await w.cog.owner_ban_dm(msg)
    c = clique(DONO)

    await msg.channel.send.await_args.kwargs["view"].confirmed(c)

    assert c.edit_original_message.await_args.kwargs["content"] == PT("erasure.ban.owner.write_failed")
    assert not w.bans.is_blocked(COMUM)


@pytest.mark.asyncio
async def test_the_panel_entry_and_its_modal_check_env_owner_ids():
    w = mundo_do_dono()
    callback = UserBansCog.userbans.callback

    intruso = interacao_de_mensagem(DONO_DA_APP)
    assert await callback(w.cog, intruso) == PT("erasure.not_yours")
    intruso.response.defer.assert_not_awaited()

    dono = interacao_de_mensagem(DONO)
    assert await callback(w.cog, dono) == PT("erasure.ban.panel.opened")
    dono.response.defer.assert_awaited_once()  # o PanelView edita a mensagem original depois
    assert isinstance(dono.followup.send.await_args.kwargs["view"], views.UserBanPanelView)
    assert dono.followup.send.await_args.kwargs["ephemeral"] is True
    assert await callback(w.cog, object()) is None  # por prefixo não faz nada

    painel = dono.followup.send.await_args.kwargs["view"]
    assert await painel.interaction_check(clique(DONO_DA_APP)) is False
    assert await painel.interaction_check(clique(DONO)) is True
    abrir = clique(DONO)
    await painel._open_ban(abrir)
    modal = abrir.response.send_modal.await_args.args[0]
    assert isinstance(modal, views.UserBanModal) and len(modal.components) == 2  # ID e motivo


@pytest.mark.asyncio
async def test_the_modal_callback_refuses_non_env_owners_and_builds_the_confirmation_for_owners():
    w = mundo_do_dono()
    modal = views.UserBanModal(w.bot, PT, "ban")

    intruso = clique(DONO_DA_APP)
    intruso.text_values = {views.ID_FIELD: str(COMUM), views.REASON_FIELD: "x"}
    await modal.callback(intruso)
    assert intruso.response.send_message.await_args.args == (PT("erasure.not_yours"),)
    w.bot.fetch_user.assert_not_awaited()

    dono = clique(DONO)
    dono.text_values = {views.ID_FIELD: f" {COMUM} ", views.REASON_FIELD: MOTIVO}
    await modal.callback(dono)
    enviado = dono.response.send_message.await_args.kwargs
    assert enviado["ephemeral"] is True and isinstance(enviado["view"], views.UserBanConfirmView)
    assert not w.bans.is_blocked(COMUM)


@pytest.mark.asyncio
async def test_the_unban_modal_has_no_reason_field_and_cancel_and_timeout_change_nothing():
    w = mundo_do_dono()
    assert len(views.UserBanModal(w.bot, PT, "unban").components) == 1
    prepared = await views.prepare_confirmation(w.bot, PT, DONO, "ban", str(COMUM), "")
    view = prepared.view
    cancelar = clique(DONO)
    await view.cancelled(cancelar)
    assert cancelar.response.edit_message.await_args.kwargs["content"] == PT("erasure.ban.owner.cancelled")
    view.sent_message = SimpleNamespace(edit=AsyncMock())
    await view.on_timeout()
    view.sent_message.edit.assert_awaited_once_with(view=None)
    assert not w.bans.is_blocked(COMUM)


# ------------------------------------------------------------------------------- R8.10: scrobble


def faixa():
    return SimpleNamespace(
        is_stream=False,
        duration=200000,
        info={"sourceName": "deezer"},
        album_name="Album",
        author="Artista",
        single_title="Musica",
        title="Musica",
        autoplay=False,
        requester=COMUM,
        ytid=None,
        url="https://x/faixa",
    )


def cog_de_scrobble(bans, *, cache=None):
    sessoes = {
        COMUM: {"scrobble": True, "sessionkey": "CHAVE-COMUM"},
        BANIDO: {"scrobble": True, "sessionkey": "CHAVE-BANIDO"},
    }
    bot = SimpleNamespace(
        pool=SimpleNamespace(
            user_bans=bans,
            lastfm_sessions=sessoes,
            erasure_tombstones=SimpleNamespace(now=lambda: 0.0, stale=lambda *a: False),
        ),
        last_fm=SimpleNamespace(update_nowplaying=AsyncMock(), track_scrobble=AsyncMock(), cache=cache or {}),
        get_global_data=AsyncMock(),
    )
    cog = SimpleNamespace(bot=bot, save_scrobble=AsyncMock())
    player = MagicMock()
    player.lastfm_users = {}
    player.guild.me.voice = object()
    return cog, player


@pytest.mark.asyncio
async def test_a_banned_listener_is_dropped_from_the_scrobble_and_the_music_goes_on():
    cog, player = cog_de_scrobble(banidos(BANIDO))
    ouvintes = [SimpleNamespace(id=BANIDO, bot=False), SimpleNamespace(id=COMUM, bot=False)]

    await LastFmCog.startscrooble(cog, player, faixa(), update_np=True, users=ouvintes)

    chamadas = cog.bot.last_fm.update_nowplaying.await_args_list
    assert [c.kwargs["session_key"] for c in chamadas] == ["CHAVE-COMUM"]
    player.stop.assert_not_called()  # D3: a música em curso é do servidor
    player.destroy.assert_not_called()


@pytest.mark.asyncio
async def test_a_banned_only_channel_scrobbles_nothing_and_reads_nothing():
    cog, player = cog_de_scrobble(banidos(BANIDO))
    player.last_channel.members = [SimpleNamespace(id=BANIDO, bot=False)]

    await LastFmCog.startscrooble(cog, player, faixa(), update_np=True)  # sem ``users``: vem do canal de voz

    cog.bot.last_fm.update_nowplaying.assert_not_awaited()
    cog.bot.get_global_data.assert_not_awaited()


@pytest.mark.asyncio
async def test_save_scrobble_never_receives_the_banned_user():
    track = faixa()
    track.info = {"sourceName": "youtube"}
    track.album_name = None  # sem álbum nem metadados: o caminho que chama ``save_scrobble``
    track.title, track.author = "titulo curto", "autor"
    consulta = f"{track.author} - {track.title}".lower()
    cog, player = cog_de_scrobble(banidos(BANIDO), cache={consulta: {}})  # sem metadados: cai no ``save_scrobble``
    ouvintes = [SimpleNamespace(id=BANIDO, bot=False), SimpleNamespace(id=COMUM, bot=False)]

    await LastFmCog.startscrooble(cog, player, track, update_np=True, users=ouvintes)

    cog.save_scrobble.assert_awaited_once()
    assert [u.id for u in cog.save_scrobble.await_args.kwargs["users"]] == [COMUM]


# ------------------------------------------------------------------------------- T-513: respostas reais e confirmação


def resposta_real():
    """``InteractionResponse`` REAL do disnake com o adaptador HTTP falso: ``embed=None`` viraria ``embeds=[None]`` e quebraria aqui."""
    from disnake.interactions.base import InteractionResponse
    from disnake.webhook.async_ import async_context

    adaptador = SimpleNamespace(create_interaction_response=AsyncMock())
    async_context.set(adaptador)
    pai = SimpleNamespace(
        id=1, token="t", _session=None, _state=SimpleNamespace(allowed_mentions=None, store_view=MagicMock())
    )
    return InteractionResponse(pai), adaptador


def modal_submetido(autor, id_texto, motivo=""):
    resposta, adaptador = resposta_real()
    inter = SimpleNamespace(
        author=SimpleNamespace(id=autor),
        text_values={views.ID_FIELD: id_texto, views.REASON_FIELD: motivo},
        response=resposta,
    )
    return inter, adaptador


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("autor", "id_texto", "esperado"),
    [
        pytest.param(DONO_DA_APP, str(COMUM), "erasure.not_yours", id="não é dono do .env"),
        pytest.param(DONO, "abc", "erasure.owner.invalid_id", id="ID inválido"),
        pytest.param(DONO, str(DONO), "erasure.ban.owner.target_owner", id="alvo dono"),
    ],
)
async def test_every_modal_refusal_goes_through_the_real_interaction_response(autor, id_texto, esperado):
    w = mundo_do_dono()
    inter, adaptador = modal_submetido(autor, id_texto)

    await views.UserBanModal(w.bot, PT, "ban").callback(inter)

    dados = adaptador.create_interaction_response.await_args.kwargs["data"]
    assert dados["content"] == PT(esperado) and dados["flags"] == 64
    assert "embeds" not in dados and "components" not in dados  # só os campos preenchidos


@pytest.mark.asyncio
async def test_the_modal_confirmation_goes_through_the_real_response_with_embed_view_and_no_mentions():
    w = mundo_do_dono()
    w.alvo.display_name = "@everyone **Fulano**"
    inter, adaptador = modal_submetido(DONO, str(COMUM), "[link](http://x) @here")

    await views.UserBanModal(w.bot, PT, "ban").callback(inter)

    dados = adaptador.create_interaction_response.await_args.kwargs["data"]
    descricao = dados["embeds"][0]["description"]
    assert dados["components"] and dados["flags"] == 64
    assert dados["allowed_mentions"] == {"parse": []}  # nada notifica ninguém
    assert "@everyone" not in descricao and "@here" not in descricao  # render_text: ``@`` desarmado
    assert "\\*\\*Fulano\\*\\*" in descricao and "\\[link\\]" in descricao  # markdown escapado (nome e motivo)


def confirmacao_do_dono(w, *, acao="ban"):
    return views.UserBanConfirmView(w.bot, PT, DONO, acao, COMUM, "Fulano", "motivo")


@pytest.mark.asyncio
async def test_another_env_owner_cannot_click_someone_elses_confirmation():
    w = mundo_do_dono()
    w.bot.env_owner_ids = {DONO, 100000000000000007}
    view = confirmacao_do_dono(w)

    assert await view.interaction_check(clique(100000000000000007)) is False  # outro dono do .env
    assert await view.interaction_check(clique(DONO)) is True


@pytest.mark.asyncio
async def test_the_owner_is_acknowledged_before_the_write_and_gets_the_final_text_after_it():
    w = mundo_do_dono()
    ordem = []
    original = w.bans.ban

    async def ban_lento(*args):
        ordem.append(("ban", view.is_finished()))  # a view já parou: um segundo clique não repete
        return await original(*args)

    w.bans.ban = ban_lento
    view = confirmacao_do_dono(w)
    c = clique(DONO)
    c.response.edit_message.side_effect = lambda **kw: ordem.append(("ack", kw["content"]))
    c.edit_original_message = AsyncMock(side_effect=lambda **kw: ordem.append(("final", kw["content"])))

    await view.confirmed(c)

    assert [o[0] for o in ordem] == ["ack", "ban", "final"]
    assert ordem[0][1] == PT("erasure.ban.owner.working") and ordem[1][1] is True
    assert ordem[2][1] == PT("erasure.ban.owner.banned", name="Fulano", id=COMUM)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("quebra", "partes"),
    [
        ("sessoes", ["erasure.ban.owner.failed_sessions"]),
        ("rpc", ["erasure.ban.owner.failed_rpc"]),
        ("ambos", ["erasure.ban.owner.failed_sessions", "erasure.ban.owner.failed_rpc"]),
    ],
)
async def test_a_partial_ban_is_shown_to_the_owner_with_what_failed(quebra, partes):
    w = mundo_do_dono()
    if quebra in ("sessoes", "ambos"):
        w.bans.pool.panel_sessions = SimpleNamespace(destroy_user=MagicMock(side_effect=RuntimeError("x")))
    if quebra in ("rpc", "ambos"):
        w.bans._rpc_closer = MagicMock(side_effect=RuntimeError("x"))
    c = clique(DONO)
    c.edit_original_message = AsyncMock()

    await confirmacao_do_dono(w).confirmed(c)

    final = c.edit_original_message.await_args.kwargs["content"]
    assert final == PT("erasure.ban.owner.partial", failed=", ".join(PT(k) for k in partes))
    assert w.bans.is_blocked(COMUM)


@pytest.mark.asyncio
async def test_a_failed_acknowledgement_still_delivers_the_result():
    w = mundo_do_dono()
    c = clique(DONO)
    c.response.edit_message.side_effect = disnake.HTTPException(MagicMock(status=500), "x")
    c.edit_original_message = AsyncMock()

    await confirmacao_do_dono(w).confirmed(c)

    assert w.bans.is_blocked(COMUM) and c.edit_original_message.await_count == 1


# ------------------------------------------------------------------------------- Q13: um limitador para o pool


@pytest.mark.asyncio
async def test_two_bots_in_the_pool_give_one_warning_per_user_per_hour():
    bans = banidos(BANIDO)
    um, outro = bot_de_mensagens(bans), bot_de_mensagens(bans)
    primeira, segunda = mensagem(BANIDO, "!!play x"), mensagem(BANIDO, "!!play x")

    await BotCore.on_message(um, primeira)
    await BotCore.on_message(outro, segunda)

    primeira.reply.assert_awaited_once()
    segunda.reply.assert_not_awaited()


@pytest.mark.asyncio
async def test_a_warning_without_permission_does_not_spend_the_quota():
    bans = banidos(BANIDO)
    sem_permissao, com_permissao = bot_de_mensagens(bans), bot_de_mensagens(bans)
    sem_permissao.can_send_message.return_value = False
    perdida, valida = mensagem(BANIDO, "!!play x"), mensagem(BANIDO, "!!play x")

    await BotCore.on_message(sem_permissao, perdida)
    await BotCore.on_message(com_permissao, valida)

    perdida.reply.assert_not_awaited()
    valida.reply.assert_awaited_once()  # o outro bot ainda pôde avisar
