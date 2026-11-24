# -*- coding: utf-8 -*-
"""SPIKE TEMPORÁRIO (T-C-BUILD): prova as incógnitas da Jukebox C em Components V2 no Discord de verdade. Sai antes do PR.

Só o dono (``admin_owner_only``) roda ``v2spike``; nada aqui toca o player nem o banco. Passos (cada resultado vai ao chat):
1. V1 -> V2 por ``message.edit`` (flag ligada, ``content=None``, ``embeds=[]``, ``attachments=[]``, 2 arquivos novos);
2. a Thumbnail aponta para um anexo mantido (troca ``.gif`` <-> ``.png`` sem reenvio) e os ids dos anexos não mudam;
3. ``attachments=[]`` + 2 arquivos de novo (a troca de capa): os ids mudam;
4. a conversão V1 -> V2 por interação (botão numa mensagem V1);
5. o pior caso de componentes (contagem e envio) e ``Container(accent_colour=disnake.Colour)``.
"""

from __future__ import annotations

import io
import logging
from typing import TYPE_CHECKING, Any

import disnake
from disnake.ext import commands
from PIL import Image, ImageDraw

from empiremuse.utils.others import CustomContext
from empiremuse.utils.owner_gate import admin_owner_only

if TYPE_CHECKING:
    from empiremuse.utils.client import BotCore

logger = logging.getLogger(__name__)

SIDE, FRAMES, FRAME_MS = 128, 24, 75
_AUTORAN = False  # o disparo automático roda uma vez por processo
VIOLET = disnake.Colour(0x8B5CF6)


def disc_frames(label: tuple[int, int, int]) -> list[Image.Image]:
    """Um disco com um marcador no rótulo, para ver o giro a olho."""
    frames = []
    for n in range(FRAMES):
        image = Image.new("RGB", (SIDE, SIDE), (0x1A, 0x12, 0x40))
        draw = ImageDraw.Draw(image)
        draw.ellipse((4, 4, SIDE - 4, SIDE - 4), fill=(18, 16, 30))
        draw.ellipse((34, 34, SIDE - 34, SIDE - 34), fill=label)
        marker = Image.new("RGBA", (SIDE, SIDE), (0, 0, 0, 0))
        ImageDraw.Draw(marker).pieslice((34, 34, SIDE - 34, SIDE - 34), -10, 25, fill=(255, 255, 255, 255))
        ImageDraw.Draw(marker).ellipse((SIDE - 24, SIDE // 2 - 4, SIDE - 14, SIDE // 2 + 6), fill=(255, 80, 120, 255))
        turned = marker.rotate(-360 * n / FRAMES, resample=Image.BICUBIC)
        image.paste(turned, (0, 0), turned)
        frames.append(image)
    return frames


def gif_bytes(label: tuple[int, int, int]) -> bytes:
    frames = disc_frames(label)
    out = io.BytesIO()
    frames[0].save(out, "GIF", save_all=True, append_images=frames[1:], duration=FRAME_MS, loop=0, disposal=2)
    return out.getvalue()


def png_bytes(label: tuple[int, int, int]) -> bytes:
    out = io.BytesIO()
    disc_frames(label)[0].save(out, "PNG")
    return out.getvalue()


def disc_files(name: str, label: tuple[int, int, int]) -> list[disnake.File]:
    return [
        disnake.File(io.BytesIO(gif_bytes(label)), filename=f"{name}.gif"),
        disnake.File(io.BytesIO(png_bytes(label)), filename=f"{name}.png"),
    ]


def tree(thumb: str, title: str) -> disnake.ui.Container:
    return disnake.ui.Container(
        disnake.ui.Section(
            disnake.ui.TextDisplay("-# ▶ TOCANDO · Lounge"),
            disnake.ui.TextDisplay(f"## [{title}](https://example.com)"),
            disnake.ui.TextDisplay("**Artista** · Álbum\n-# pedida por você"),
            accessory=disnake.ui.Thumbnail(media=f"attachment://{thumb}", description="Disco do EmpireMuse"),
        ),
        disnake.ui.TextDisplay("`1:12` ▰▰▰▰●▱▱▱▱▱▱▱ `3:29` · termina <t:1900000000:R>"),
        disnake.ui.Separator(divider=True, spacing=disnake.SeparatorSpacing.small),
        disnake.ui.ActionRow(
            disnake.ui.Button(emoji="⏯", custom_id="v2spike_dead_1", disabled=True),
            disnake.ui.Button(emoji="⏭", custom_id="v2spike_dead_2", disabled=True),
        ),
        accent_colour=VIOLET,
    )


def count_components(components: list[Any]) -> int:
    def walk(node: Any) -> int:
        if isinstance(node, dict):
            return (1 if "type" in node else 0) + sum(walk(v) for v in node.values())
        if isinstance(node, list):
            return sum(walk(v) for v in node)
        return 0

    return sum(walk(c.to_component_dict()) for c in components)


def worst_case() -> disnake.ui.Container:
    """O pior caso da spec §1: Section(3 textos + Thumbnail), tempo, chips, Separator, fila, Separator, 3 ActionRow, ação."""
    options = [disnake.SelectOption(label=f"{n}. faixa", value=f"{n:02d}.x") for n in range(25)]

    def select(custom_id: str) -> disnake.ui.StringSelect:
        return disnake.ui.StringSelect(custom_id=custom_id, options=options, min_values=0, max_values=1)

    return disnake.ui.Container(
        disnake.ui.Section(
            disnake.ui.TextDisplay("-# a"),
            disnake.ui.TextDisplay("## b"),
            disnake.ui.TextDisplay("c"),
            accessory=disnake.ui.Thumbnail(media="attachment://disc.gif"),
        ),
        disnake.ui.TextDisplay("tempo"),
        disnake.ui.TextDisplay("chips"),
        disnake.ui.Separator(),
        disnake.ui.TextDisplay("fila " + "x" * 1500),
        disnake.ui.Separator(divider=False),
        disnake.ui.ActionRow(*[disnake.ui.Button(emoji="⏯", custom_id=f"v2spike_w{n}") for n in range(5)]),
        disnake.ui.ActionRow(select("v2spike_s1")),
        disnake.ui.ActionRow(select("v2spike_s2")),
        disnake.ui.TextDisplay("-# ação"),
        accent_colour=VIOLET,
    )


def describe(message: disnake.Message) -> str:
    files = ", ".join(f"{a.filename}={a.id}" for a in message.attachments) or "nenhum"
    thumbs = []
    for component in message.components:
        for child in getattr(component, "children", []):
            media = getattr(getattr(child, "accessory", None), "media", None)
            if media is not None:
                name = media.url.rsplit("/", 1)[-1].split("?")[0]
                thumbs.append(f"{name} ({media.content_type}, {media.width}x{media.height})")
    return (
        f"V2={message.flags.is_components_v2} · content={message.content!r} · embeds={len(message.embeds)} · "
        f"anexos: {files} · Thumbnail: {', '.join(thumbs) or '—'}"
    )


class ConvertView(disnake.ui.View):
    def __init__(self) -> None:
        super().__init__(timeout=600)

    @disnake.ui.button(label="Converter por interação (V1→V2)", style=disnake.ButtonStyle.primary)
    async def convert(self, _button: disnake.ui.Button, inter: disnake.MessageInteraction) -> None:
        try:
            await inter.response.edit_message(
                content=None,
                embeds=[],
                attachments=[],
                components=[tree("disc-i.gif", "Interação")],
                files=disc_files("disc-i", (220, 160, 40)),
            )
            note = "OK"
        except Exception as error:  # noqa: BLE001 - o spike só relata
            note = f"FALHOU: {type(error).__name__}: {str(error)[:300]}"
        message = await inter.channel.fetch_message(inter.message.id)
        await inter.channel.send(f"**Passo 4, V1→V2 por interação:** {note}\n{describe(message)}")


class V2Spike(commands.Cog):
    def __init__(self, bot: BotCore):
        self.bot = bot

    @commands.Cog.listener("on_ready")
    async def autorun(self) -> None:
        """Com ``V2SPIKE_CHANNEL_ID`` na config, roda os passos uma vez por processo no canal (sem a chave, não faz nada)."""
        global _AUTORAN
        raw = str(self.bot.config.get("V2SPIKE_CHANNEL_ID") or "").strip()
        if _AUTORAN or not raw.isdigit():
            return
        channel = self.bot.get_channel(int(raw))
        if channel is None:
            return  # outro bot do pool enxerga o canal: ele roda
        _AUTORAN = True
        try:
            await self.run(channel)
        except Exception:
            logger.exception("v2spike autorun falhou")

    @admin_owner_only()
    @commands.max_concurrency(1, commands.BucketType.default)
    @commands.command(name="v2spike", hidden=True, description="Spike temporário da Jukebox C (Components V2).")
    async def v2spike(self, ctx: CustomContext):
        await self.run(ctx.channel)

    async def run(self, channel: Any) -> None:
        report: list[str] = []

        async def step(name: str, coro) -> None:
            try:
                await coro
                report.append(f"**{name}:** OK")
            except Exception as error:  # noqa: BLE001 - o spike só relata
                report.append(f"**{name}:** FALHOU {type(error).__name__}: {str(error)[:300]}")
                logger.warning("v2spike %s: %s", name, error)

        v1 = await channel.send(
            "Mensagem V1 (content + embed + anexo)",
            embed=disnake.Embed(title="V1", description="vai virar V2"),
            file=disnake.File(io.BytesIO(png_bytes((60, 120, 220))), filename="velho.png"),
        )
        await step(
            "1 V1→V2 por edit (flag, content/embeds/attachments zerados, GIF+PNG)",
            v1.edit(
                content=None,
                embeds=[],
                attachments=[],
                components=[tree("disc-a.gif", "Passo 1")],
                files=disc_files("disc-a", (200, 60, 60)),
            ),
        )
        after = await channel.fetch_message(v1.id)
        report.append(describe(after))
        ids_before = {a.filename: a.id for a in after.attachments}

        await step(
            "2a pausa (Thumbnail→.png mantido, sem arquivo)", after.edit(components=[tree("disc-a.png", "Pausada")])
        )
        paused = await channel.fetch_message(v1.id)
        report.append(describe(paused))
        await step(
            "2b retoma (Thumbnail→.gif mantido, sem arquivo)", paused.edit(components=[tree("disc-a.gif", "Retomada")])
        )
        resumed = await channel.fetch_message(v1.id)
        report.append(describe(resumed))
        report.append(
            f"ids dos anexos iguais depois de pausa e retomada: {ids_before == {a.filename: a.id for a in resumed.attachments}}"
        )

        await step(
            "3 troca de capa (attachments=[] + GIF+PNG novos)",
            resumed.edit(
                attachments=[], components=[tree("disc-b.gif", "Passo 3")], files=disc_files("disc-b", (60, 200, 90))
            ),
        )
        report.append(describe(await channel.fetch_message(v1.id)))

        worst = worst_case()
        report.append(f"pior caso da spec: {count_components([worst])} de 40 componentes")
        await step(
            "5 pior caso enviado (accent_colour=Colour)",
            channel.send(
                components=[worst], files=[disnake.File(io.BytesIO(gif_bytes((90, 90, 220))), filename="disc.gif")]
            ),
        )

        await channel.send(
            "**Passo 4:** clique para converter esta mensagem V1 em V2 por interação.", view=ConvertView()
        )
        await channel.send("**Resultado do spike V2 (T-C-BUILD)**\n" + "\n".join(report))


def setup(bot: BotCore) -> None:
    bot.add_cog(V2Spike(bot))
