from __future__ import annotations

from typing import TYPE_CHECKING, NamedTuple, Optional, Union

import disnake
from disnake.ext import commands

from empiremuse.utils.music.errors import GenericError
from empiremuse.utils.others import CustomContext
from empiremuse.utils.music.app_emojis import emoji as app_emoji

if TYPE_CHECKING:
    from empiremuse.rules.i18n import Translator
    from empiremuse.utils.client import BotCore

category_icons = {}

# O id do catálogo (``cmd.<id>.{name,description,aliases}``, contrato-nomes-radios §5) de cada comando de prefixo cujo nome difere
# dele. Os outros usam o próprio nome qualificado com ``.`` no lugar do espaço (``jukebox setup`` → ``jukebox.setup``).
PREFIX_CATALOG_IDS = {
    "adddj": "add_dj",
    "adddjrole": "add_dj_role",
    "changeskin": "change_skin",
    "clear": "queue.clear",
    "commandlog": "command_log",
    "customskin": "custom_skin",
    "favmanager": "fav_manager",
    "loop": "loop_mode",
    "nowplaying": "now_playing",
    "playersettings": "player_settings",
    "queue": "queue.display",
    "readd": "readd_songs",
    "removedjrole": "remove_dj_role",
    "restrictmode": "restrict_mode",
    "reverse": "queue.reverse",
    "richpresence": "rich_presence",
    "savequeue": "save_queue",
    "setvoicestatus": "set_voice_status",
    "shuffle": "queue.shuffle",
}
NO_DESCRIPTION = "Sem descrição..."


class CommandTexts(NamedTuple):
    name: str
    description: str
    aliases: tuple[str, ...]


def catalog_id(cmd: commands.Command) -> str:
    return PREFIX_CATALOG_IDS.get(cmd.qualified_name, cmd.qualified_name.replace(" ", "."))


def catalog_text(tr: Translator, cmd: commands.Command, field: str) -> Optional[str]:
    """O texto do catálogo no idioma do tradutor; ``None`` quando a chave não existe (o ``Translator`` devolve a própria chave)."""
    key = f"cmd.{catalog_id(cmd)}.{field}"
    text = tr(key)
    return None if text == key else text


def command_texts(tr: Translator, cmd: commands.Command) -> CommandTexts:
    """Nome, descrição e aliases do comando no idioma do servidor; um comando fora do catálogo cai no texto do decorador."""
    aliases = catalog_text(tr, cmd, "aliases")
    return CommandTexts(
        name=catalog_text(tr, cmd, "name") or cmd.name,
        description=catalog_text(tr, cmd, "description") or cmd.description or NO_DESCRIPTION,
        aliases=tuple(a.strip() for a in aliases.split(",") if a.strip())
        if aliases is not None
        else tuple(cmd.aliases),
    )


def listing_entry(tr: Translator, cmd: commands.Command) -> str:
    """``nome (alias, alias)`` na lista de uma categoria: o nome é o que se digita; os aliases vêm do catálogo."""
    aliases = command_texts(tr, cmd).aliases
    return f"{cmd.name} ({', '.join(aliases)})" if aliases else cmd.name


def invocation(ctx, cmd: commands.Command, word: str) -> str:
    """Como se digita ``word`` (o nome ou um alias) do comando: um subcomando leva o grupo na frente."""
    parent = f"{cmd.full_parent_name} " if cmd.full_parent_name else ""
    return f"{ctx.prefix}{parent}{word}"


class ViewHelp(disnake.ui.View):
    def __init__(self, ctx, items, *, get_cmd, main_embed, cmd_list, category_cmd=None, timeout=180):
        self.message: Optional[disnake.Message] = None
        self.page_index = 0
        self.cmd_lst = cmd_list
        self.category = category_cmd
        self.get_cmd = get_cmd
        self.items = items
        self.ctx = ctx
        self.main_embed = main_embed
        self.first_embed = main_embed
        super().__init__(timeout=timeout)
        self.process_buttons()

    async def interaction_check(self, interaction: disnake.Interaction):
        if interaction.user != self.ctx.author:
            await interaction.response.send_message(
                f"Apenas o membro {self.ctx.author.mention} pode usar essas opções.", ephemeral=True
            )
            return

        return True

    def process_buttons(self):
        options = []

        for category, emoji in self.items:
            b = disnake.SelectOption(
                label=category,
                value=category,
                emoji=emoji,
                default=category == self.category,
                description="Ver detalhes dos comandos desta categoria.",
            )

            options.append(b)

        if options:
            sel = disnake.ui.Select(placeholder="Escolha uma categoria para ver todos os comandos:", options=options)
            sel.callback = self.callback_help
            self.add_item(sel)

        if self.category:
            if len(self.cmd_lst[self.category]["cmds"]) > 1:
                left_button = disnake.ui.Button(
                    style=disnake.ButtonStyle.grey, emoji=app_emoji(self.ctx.bot, "arrow_left"), custom_id="left_page"
                )
                left_button.callback = self.callback_left
                self.add_item(left_button)

                right_button = disnake.ui.Button(
                    style=disnake.ButtonStyle.grey, emoji=app_emoji(self.ctx.bot, "arrow_right"), custom_id="right_page"
                )
                right_button.callback = self.callback_right
                self.add_item(right_button)

            back_button = disnake.ui.Button(
                style=disnake.ButtonStyle.grey,
                emoji=app_emoji(self.ctx.bot, "leftwards_arrow_with_hook"),
                custom_id="back_page",
                label="Voltar",
            )
            back_button.callback = self.callback_back
            self.add_item(back_button)

    async def response(self, interaction):
        if not self.category and not self.page_index:
            self.clear_items()
            self.process_buttons()

        self.main_embed = await self.get_cmd(
            ctx=self.ctx,
            index=self.page_index,
            cmds=self.cmd_lst[self.category]["cmds"],
            emoji=self.cmd_lst[self.category]["emoji"],
            category=self.category,
        )

        await interaction.response.edit_message(embed=self.main_embed, view=self)

    async def callback_left(self, interaction):
        if self.page_index == 0:
            self.page_index += len(self.cmd_lst[self.category]["cmds"]) - 1
        else:
            self.page_index -= 1

        await self.response(interaction)

    async def callback_right(self, interaction):
        if self.page_index == len(self.cmd_lst[self.category]["cmds"]) - 1:
            self.page_index -= len(self.cmd_lst[self.category]["cmds"]) - 1
        else:
            self.page_index += 1

        await self.response(interaction)

    async def callback_back(self, interaction):
        self.page_index = 0
        self.category = None
        self.clear_items()
        self.process_buttons()

        await interaction.response.edit_message(embed=self.first_embed, view=self)

    async def callback_help(self, interaction: disnake.MessageInteraction):
        self.category = interaction.data.values[0]

        self.page_index = 0
        self.clear_items()
        self.process_buttons()

        self.main_embed = await self.get_cmd(
            ctx=self.ctx,
            index=self.page_index,
            cmds=self.cmd_lst[self.category]["cmds"],
            emoji=self.cmd_lst[self.category]["emoji"],
            category=self.category,
        )

        await interaction.response.edit_message(embed=self.main_embed, view=self)


async def check_perms(ctx: CustomContext, cmd: commands.Command):
    try:
        if cmd.hidden and not await ctx.bot.is_owner(ctx.author):
            return False
    except Exception:
        return False

    return True


def check_cmd(cmd: commands.command):
    if hasattr(cmd, "category") and cmd.category:
        return True


class HelpCog(commands.Cog, name="Ajuda"):
    def __init__(self, bot: BotCore):
        self.bot = bot
        self._original_help_command = bot.help_command
        bot.remove_command("help")
        self.task_users = {}
        self.mention_cd = commands.CooldownMapping.from_cooldown(1, 30, commands.BucketType.channel)

    async def get_cmd(self, ctx, cmds, index=0, category=None, emoji=None):
        cmd = cmds[index]

        tr = await self.bot.pool.i18n.for_guild(ctx.guild)
        texts = command_texts(tr, cmd)

        prefix = ctx.prefix if str(ctx.me.id) not in ctx.prefix else f"@{ctx.me.display_name} "

        if cmd.usage:
            usage_cmd = (
                cmd.usage.replace("{prefix}", prefix)
                .replace("{cmd}", cmd.name)
                .replace("{parent}", cmd.full_parent_name)
                .replace(f"<@!{ctx.bot.user.id}>", f"@{ctx.me.name}")
                .replace(f"<@{ctx.bot.user.id}>", f"@{ctx.me.name}")
            )
        else:
            usage_cmd = ""

        embed = disnake.Embed(color=self.bot.get_color(ctx.guild.me))

        title = f"{ctx.prefix}{cmd}" if texts.name == cmd.name else f"{ctx.prefix}{cmd} ({texts.name})"
        txt = f"### ⌨️ ⠂Comando: {title}\n```\n{texts.description}```\n"
        if texts.aliases:
            aliases = " | ".join(invocation(ctx, cmd, ali) for ali in texts.aliases)
            txt += f"🔄 **⠂Alternativas:** ```\n{aliases}```\n"
        if hasattr(cmd, "commands"):
            subs = " | ".join([c.name for c in cmd.commands if (await check_perms(ctx, c))])
            txt += f"🔢 **⠂Subcomandos:** ```{subs}``` Use o comando: `[ {ctx.prefix}help {cmd} subcomando ]` para ver mais detalhes do subcomando.\n\n"

        if usage_cmd:
            txt += (
                f"📘 **⠂Como Usar:** ```\n{usage_cmd}```\n"
                f"⚠️ **⠂Notas sobre o uso dos argumentos no comando:** ```\n"
                f"[] = Obrigatório | <> = Opcional```\n"
            )

        flags = cmd.extras.get("flags")

        if flags and (actions := flags._actions):
            t = []

            for a in actions:
                # if a.hidden:
                #    continue

                if not a.help or not a.option_strings:
                    continue

                s = " ".join(i for i in a.option_strings)

                s = f"[{s}] {a.help}"

                # s += f" = `{a.help}`"

                # if a.default is False:
                # s += " `Padrão: Desativado`"
                # elif a.default is True:
                # s += " `Padrão: Ativado`"
                # elif not a.default is None:
                # s += f" `Padrão: {a.default}`"
                t.append(s)

            if t:
                txt += "🚩 **⠂Flags `(opções para adicionar no final do comando)`:**```ini\n" + "\n\n".join(t) + "```"

        embed.set_author(name="Menu de ajuda - Lista de comandos (prefix)", icon_url=self.bot.user.display_avatar.url)

        embed.description = txt

        appinfo = ctx.bot.appinfo
        try:
            owner = appinfo.team.owner
        except AttributeError:
            owner = appinfo.owner

        if (max_pages := len(cmds)) > 1:
            embed.set_footer(
                icon_url=owner.display_avatar.replace(static_format="png"), text=f"Página: {index + 1} de {max_pages}"
            )
        return embed

    @commands.cooldown(2, 5, commands.BucketType.user)
    @commands.max_concurrency(1, commands.BucketType.user)
    @commands.command(hidden=True, name="help", aliases=["ajuda"])
    async def _help(self, ctx, *cmd_name):
        if cmd_name:
            await self.parse_direct(ctx, list(cmd_name))
            return

        embed, btn_id, cmd_lst_new = await self.overview(ctx)

        view = ViewHelp(
            ctx, btn_id, get_cmd=self.get_cmd, cmd_list=cmd_lst_new, category_cmd=None, main_embed=embed, timeout=180
        )

        view.message = await ctx.send(embed=embed, mention_author=False, view=view)

        await view.wait()

        eb = view.main_embed
        eb.clear_fields()

        for item in view.children:
            if isinstance(item, (disnake.ui.Button, disnake.ui.Select)):
                item.disabled = True

        try:
            await view.message.edit(embed=eb, view=view)
        except disnake.NotFound:
            pass

    async def overview(self, ctx):
        """A primeira página do ``help``: cada categoria com os comandos visíveis e seus aliases (catálogo, no idioma do servidor).

        Devolve o embed, a lista ``[categoria, emoji]`` do select e o dicionário ``{categoria: {"emoji", "cmds"}}`` das páginas.
        """
        tr = await self.bot.pool.i18n.for_guild(ctx.guild)
        cmdlst = {}

        for cmd in sorted(ctx.bot.commands, key=lambda c: c.name):
            if not await check_perms(ctx, cmd):
                continue

            if check_cmd(cmd):
                category_icon = category_icons.get(cmd.category)
            else:
                category_icon = None

            if category_icon:
                if not category_icon in cmdlst:
                    cmdlst[category_icon] = (cmd.category, [])
                cmdlst[category_icon][1].append(cmd)

            elif not cmd.cog or not hasattr(cmd.cog, "name") or len(cmd.cog.get_commands()) < 2:
                if not "🔰" in cmdlst:
                    cmdlst["🔰"] = ("Diversos", [])
                cmdlst["🔰"][1].append(cmd)

            else:
                if not cmd.cog.emoji:
                    cmd.cog.emoji = "⁉"
                    cmd.cog.name = "Sem Categoria"
                if not cmd.cog.emoji in cmdlst:
                    cmdlst[cmd.cog.emoji] = (cmd.cog.name, [])
                cmdlst[cmd.cog.emoji][1].append(cmd)

        lst = []

        btn_id = []

        cmd_lst_new = {}

        for icon, data in cmdlst.items():
            cmd_lst_new[data[0]] = {"emoji": icon, "cmds": data[1]}

        for category, data in sorted(cmd_lst_new.items()):
            btn_id.append([category, data["emoji"]])

            cmds = ", ".join(listing_entry(tr, c) for c in sorted(data["cmds"], key=lambda c: c.name))
            n = len(data["cmds"])
            lst.append(f"\n\n**{data['emoji']} ⠂{category} ({n} comando{'s' if n > 1 else ''}):**\n`{cmds}`")

        txt = (
            f"{''.join(lst)}\n\n"
            "Para obter informações de um comando diretamente, use: \n"
            f"`{ctx.prefix}{ctx.invoked_with} <comando/alias>`"
        )

        embed = disnake.Embed(
            description=txt.replace(ctx.me.mention, f"@{ctx.me.display_name}").replace(
                f"<@!{ctx.bot.user.id}>", f"@{ctx.me.display_name}"
            ),
            color=self.bot.get_color(ctx.guild.me),
        )
        embed.set_author(
            name=f"Menu de ajuda - Lista de comandos (prefix)",
            icon_url=self.bot.user.display_avatar.replace(static_format="png").url,
        )

        try:
            owner = self.bot.appinfo.team.owner
        except AttributeError:
            owner = self.bot.appinfo.owner

        embed.set_footer(
            icon_url=owner.display_avatar.replace(static_format="png").url, text=f"Dono(a): {owner} [{owner.id}]"
        )

        return embed, btn_id, cmd_lst_new

    async def parse_direct(self, ctx: CustomContext, cmd_name: list):
        # TODO: corrigir modo recursivo de subcommands
        cmd: Union[commands.command, commands.Group] = None
        for cname in cmd_name:
            if cmd:
                if hasattr(cmd, "commands"):
                    c = cmd.get_command(cname)
                    if not c:
                        break
                    else:
                        cmd = c
            else:
                cmd = ctx.bot.get_command(cname)
                if not hasattr(cmd, "commands"):
                    break

        if not cmd or (not await check_perms(ctx, cmd)):
            b = "`" if len(cmd_name) > 1 else ""
            raise GenericError(
                f"Comando [{b}{' '.join(cmd_name[:-1])}{b}{' ' if len(cmd_name) > 1 else ''}**{cmd_name[-1]}**] não encontrado!"
            )

        if any(c for c in cmd.cog.get_commands() if check_cmd(c)):
            name = cmd.category if cmd.category else cmd.cog.name
            emoji = category_icons.get(name) or cmd.cog.emoji
            cmds = [
                c
                for c in sorted(ctx.bot.commands, key=lambda cm: cm.name)
                if await check_perms(ctx, c)
                and (hasattr(c.cog, "name") and not c.category and c.cog.name == name)
                or (hasattr(c, "category") and c.category == name)
            ]
            try:
                index = cmds.index(cmd)
            except Exception:
                cmds = [cmd]
                index = 0
        else:
            cog = ctx.bot.get_cog(cmd.cog_name)
            name = cog.name if hasattr(cog, "name") else "Diversos"
            emoji = cog.emoji if hasattr(cog, "emoji") else "🔰"

            cmds = [
                c
                for c in sorted(cog.get_commands(), key=lambda cm: cm.name)
                if await check_perms(ctx, c) or not c.hidden
            ]
            try:
                index = cmds.index(cmd)
            except Exception:
                cmds = [cmd]
                index = 0

        await ctx.reply(
            ctx.author.mention,
            embed=await self.get_cmd(ctx=ctx, cmds=cmds, index=index, category=name, emoji=emoji),
            mention_author=False,
            fail_if_not_exists=False,
        )

    async def add_reactions(self, msg: disnake.Message, reactions):
        for e in reactions:
            await msg.add_reaction(e)

    def cog_unload(self):
        self.bot.help_command = self._original_help_command


def setup(bot: BotCore):
    bot.add_cog(HelpCog(bot))
