"""B7: ``site-data.json`` (comandos públicos para o site). O que importa: roda sem rede, bate com o schema, nunca vaza comando de dono,
oculto, id interno nem o nome do cog, as permissões são as do USUÁRIO, só o ``en-US`` e o ``pt-BR`` são abertos e a saída é estável.

O exportador instala o ``I18n`` único do processo antes de importar os cogs; por isso a saída vem sempre de um **processo novo**
(o processo do pytest já importou cogs com o ``I18n`` completo e o exportador, de propósito, recusa rodar assim)."""

import json
import re
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

import pytest
from disnake.ext import commands
from jsonschema import Draft202012Validator

from empiremuse import site_data
from empiremuse.site_data import SiteDataError, is_public, main, render, validated_version

pytestmark = pytest.mark.critical

ROOT = Path(__file__).resolve().parents[2]
SCHEMA = json.loads((ROOT / "docs" / "api" / "site-data.schema.json").read_text("utf-8"))
SNOWFLAKE = re.compile(r"\d{17,20}")
# Comandos de dono conhecidos (e os aliases deles): escritos à mão de propósito, para o teste não depender do filtro que ele vigia.
OWNER_IDS_AND_ALIASES = {
    "shell", "hardreset", "reload", "reloadconfig", "reloadskins", "panel", "cleardm", "getlavaservers", "restartlavalink",
    "setavatar", "setbanner", "setguildprefix", "update", "updatelavalink", "saveplayers", "servers", "userbans", "ytoauth",
    "premium", "pauseall", "resumeall", "pausartodas", "retomartodas", "radio.premium", "radio.pauseall", "radio.resumeall",
    "radio.bot", "help", "ajuda",
}  # fmt: skip


def run_python(code: str) -> subprocess.CompletedProcess[str]:
    """Um processo novo na raiz do repositório (``-c``: a raiz entra no ``sys.path``)."""
    return subprocess.run(
        [sys.executable, "-c", textwrap.dedent(code)], cwd=ROOT, capture_output=True, text=True, check=False
    )


@pytest.fixture(scope="module")
def data():
    run = subprocess.run(
        [sys.executable, "-m", "empiremuse.site_data", "--bot-version", "2.3.0-rc.1"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert run.returncode == 0, run.stderr
    return json.loads(run.stdout)


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get("subcommands", ()))


def every_command(data):
    return list(flatten([*data["slash_commands"], *data["prefix_commands"]]))


def command(data, kind, id_):
    return next(node for node in flatten(data[f"{kind}_commands"]) if node["id"] == id_)


class TestOffline:
    def test_builds_without_network_or_discord(self):
        run = run_python(
            """
            import socket
            def denied(*_a, **_k):
                raise AssertionError("o exportador não pode usar a rede")
            socket.socket.connect = denied
            socket.getaddrinfo = denied
            from empiremuse.site_data import build_site_data
            print(len(build_site_data("dev")["slash_commands"]))
            """
        )

        assert run.returncode == 0, run.stderr
        assert int(run.stdout) > 0


class TestShape:
    def test_it_matches_the_published_schema(self, data):
        Draft202012Validator.check_schema(SCHEMA)

        Draft202012Validator(SCHEMA).validate(data)

    def test_the_schema_requires_kind_and_permissions_on_the_top_level_only(self, data):
        validator = Draft202012Validator(SCHEMA)
        top = json.loads(json.dumps(data))
        del top["slash_commands"][0]["kind"]
        assert not validator.is_valid(top)
        top = json.loads(json.dumps(data))
        del top["slash_commands"][0]["permissions"]
        assert not validator.is_valid(top)
        sub = json.loads(json.dumps(data))
        next(n for n in sub["slash_commands"] if "subcommands" in n)["subcommands"][0]["kind"] = "slash"
        assert not validator.is_valid(sub)  # o subcomando não tem kind

    def test_it_is_stable_and_has_no_dates(self, data):
        text = render(data)

        assert text.endswith("}\n") and not re.search(r"20\d\d-\d\d-\d\d", text)
        assert list(data) == ["schema_version", "bot_version", "locales", "slash_commands", "prefix_commands"]

    def test_the_catalog_content_is_there_in_both_languages(self, data):
        play = command(data, "slash", "play")
        assert {o["required"] for o in play["options"]} >= {True, False} and play["name"]["pt-BR"] != ""
        assert command(data, "slash", "queue.display")["path"] == ["queue", "display"]  # subcomando
        assert command(data, "slash", "language")["category"]["label"] == {
            "pt-BR": "Configurações",
            "en-US": "Settings",
        }
        assert command(data, "prefix", "radio")["aliases"] == {"pt-BR": ["rd", "247"], "en-US": ["rd", "247"]}
        assert command(data, "prefix", "now_playing")["path"] == ["nowplaying"]  # PREFIX_CATALOG_IDS

    @pytest.mark.parametrize("version", ["2.3.0", "2.3.0-rc.1", "dev"])
    def test_a_release_version_is_accepted(self, version):
        assert validated_version(version) == version

    @pytest.mark.parametrize("version", ["", "v2.3.0", "2.3", "2.3.0-beta", "2.3.0\n", "x; rm -rf /"])
    def test_anything_else_is_refused(self, version):
        with pytest.raises(SiteDataError):
            validated_version(version)


class TestPermissionsAreTheUsers:
    """A exigência do bot (``bot_has_guild_permissions``) não é do usuário; a do usuário vem das verificações e do ``default_member_permissions``."""

    def test_the_bots_own_requirements_never_show(self, data):
        for kind in ("slash", "prefix"):
            assert command(data, kind, "play")["permissions"] == []
            assert command(data, kind, "search")["permissions"] == []
        assert command(data, "prefix", "addposition")["permissions"] == []  # send_messages é do bot

    def test_what_the_user_needs_shows_on_commands_and_on_slash_subcommands(self, data):
        assert command(data, "slash", "language")["permissions"] == ["manage_guild"]  # default_member_permissions
        assert command(data, "prefix", "setprefix")["permissions"] == ["manage_guild"]  # has_guild_permissions
        for kind in ("slash", "prefix"):
            assert command(data, kind, "jukebox.setup")["permissions"] == ["manage_guild"]
            assert command(data, kind, "jukebox.reset")["permissions"] == ["manage_guild"]  # manage_threads é do bot

    def test_the_radio_prefix_publishes_manage_guild_and_never_the_owner_alternative(self, data):
        for id_ in ("radio", "radio.status", "radio.on", "radio.off", "radio.reset", "radio.setup"):
            assert command(data, "prefix", id_)["permissions"] == ["manage_guild"], id_
        ids = {node["id"] for node in flatten(data["prefix_commands"])}
        assert ids.isdisjoint({"radio.premium", "radio.pauseall", "radio.resumeall"})  # o dono continua de fora

    def test_only_the_users_checks_count_with_the_real_decorators(self):
        async def handler(ctx):
            return None

        for decorator in (
            commands.has_guild_permissions(manage_guild=True, kick_members=False),
            commands.has_permissions(manage_roles=True),
            commands.bot_has_guild_permissions(manage_threads=True),
            commands.bot_has_permissions(send_messages=True),
        ):
            decorator(handler)
        user_checks = [c for c in handler.__commands_checks__ if c.__qualname__.startswith(("has_", "bot_has_"))]
        assert len(user_checks) == 4  # os quatro decoradores deixaram a verificação

        assert site_data._user_permissions(SimpleNamespace(checks=handler.__commands_checks__)) == [
            "manage_guild",
            "manage_roles",
        ]


class TestNothingPrivateLeaks:
    @pytest.mark.parametrize(
        "command_like",
        [
            SimpleNamespace(hidden=True, checks=[]),
            SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__="admin_owner_only.<locals>.predicate")]),
            SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__="is_owner.<locals>.predicate")]),
            SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__="radio_owner_only")]),
        ],
    )
    def test_the_filter_drops_hidden_and_owner_commands(self, command_like):
        assert is_public(command_like) is False

    @pytest.mark.parametrize(
        "name",
        ["check_voice.<locals>.p", "RadioPrefixCommands.<lambda>", "my_radio_owner_only_x", "not_is_owner.<locals>.p"],
    )
    def test_a_plain_check_passes_the_filter_and_the_name_match_is_exact(self, name):
        assert is_public(SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__=name)]))

    def test_the_real_radio_owner_commands_are_not_public_even_without_hidden(self, monkeypatch):
        radio = next(cls for cls in site_data._cog_classes() if cls.__cog_name__ == "Radio")
        group = next(c for c in radio.__cog_commands__ if c.qualified_name == "radio")
        owner = [sub for sub in group.commands if sub.name in {"premium", "pauseall", "resumeall"}]
        assert {sub.name for sub in owner} == {"premium", "pauseall", "resumeall"}
        for sub in owner:
            assert any(check.__qualname__ == "radio_owner_only" for check in sub.checks), sub.name
            monkeypatch.setattr(sub, "hidden", False)  # só a verificação nomeada tem de bastar
            assert is_public(sub) is False, sub.name
        assert (
            is_public(group) is True
        )  # o grupo é público (a gerência do servidor usa); só os subcomandos de dono saem

    def test_a_slash_subcommand_with_an_owner_check_is_dropped_and_one_without_a_registered_object_too(self):
        import disnake

        sub = disnake.OptionType.sub_command
        body = SimpleNamespace(
            name="grupo",
            description="[x Y] | Grupo",
            name_localizations=None,
            description_localizations=None,
            options=[
                disnake.Option("publico", "Aberto", type=sub),
                disnake.Option("dono", "So do dono", type=sub),
                disnake.Option("orfao", "Sem objeto", type=sub),
            ],
        )
        public = SimpleNamespace(hidden=False, checks=[], children={})
        owner = SimpleNamespace(
            hidden=False, checks=[SimpleNamespace(__qualname__="admin_owner_only.<locals>.predicate")], children={}
        )
        command = SimpleNamespace(
            checks=[], children={"publico": public, "dono": owner}
        )  # "orfao" não tem objeto: falha fechado

        node = site_data._slash_node(["grupo"], body, command)

        assert [child["id"] for child in node["subcommands"]] == ["grupo.publico"]

    def test_no_known_owner_command_or_alias_is_exported(self, data):
        exported = {node["id"] for node in every_command(data)}
        for node in every_command(data):
            exported.update(node["path"])
            if "aliases" in node:
                exported.update(alias for aliases in node["aliases"].values() for alias in aliases)
                exported.add(".".join(node["path"]))

        assert exported & OWNER_IDS_AND_ALIASES == set()

    def test_every_hidden_or_owner_command_in_the_code_is_absent(self, data):
        """Recalcula, de forma independente do filtro, quem é de dono no código (oculto ou com verificação de dono) e exige que fique de fora."""
        exported = {node["id"] for node in flatten(data["prefix_commands"])}
        private = []
        for cls in site_data._cog_classes():
            for cmd in getattr(cls, "__cog_commands__", []):
                stack = [cmd]
                while stack:
                    item = stack.pop()
                    stack.extend(getattr(item, "commands", ()))
                    owner = any(
                        c.__qualname__ == "radio_owner_only" or c.__qualname__.startswith("admin_owner_only.")
                        for c in item.checks
                    )
                    if item.hidden or owner:
                        private.append(site_data.catalog_id(item))
        assert private, "o teste perdeu de vista os comandos de dono do código"

        assert set(private) & exported == set()

    def test_no_internal_id_or_private_key_in_the_output(self, data):
        text = render(data)

        assert not SNOWFLAKE.search(text)
        assert not any(key in text for key in ('"cog"', '"_cog"', '"_category"', "OWNER_IDS"))


class TestOnlyTheCommittedCatalogs:
    PROBE = """
        import json, shutil, sys, tempfile
        from pathlib import Path
        from empiremuse.i18n import loader

        tmp = Path(tempfile.mkdtemp())
        for locale in ("en-US", "pt-BR", "fr", "de"):  # fr e de: como os que o Crowdin baixa
            shutil.copytree(loader.DEFAULT_ROOT / ("en-US" if locale in ("fr", "de") else locale), tmp / locale)
            if locale in ("fr", "de"):
                for name in ("commands", "bot"):
                    file = tmp / locale / (name + ".json")
                    file.write_text(file.read_text("utf-8").replace('"Show information about me."', '"MARCADOR-DO-CROWDIN"'), "utf-8")
        loader.DEFAULT_ROOT = tmp
        opened = set()
        real = loader.read_catalog
        loader.read_catalog = lambda path: opened.add(Path(path).parent.name) or real(path)
        PRELUDE
        from empiremuse.site_data import build_site_data, render
        text = render(build_site_data("dev"))
        print(json.dumps({"opened": sorted(opened), "marker": "MARCADOR-DO-CROWDIN" in text}))
    """

    def test_no_other_locale_directory_is_opened_by_the_whole_process(self):
        """O ``localized()`` dos cogs chama ``get_i18n()`` na importação: o singleton restrito tem de existir ANTES, senão ele abre tudo."""
        run = run_python(self.PROBE.replace("PRELUDE", ""))

        assert run.returncode == 0, run.stderr
        report = json.loads(run.stdout)
        assert report["opened"] == ["en-US", "pt-BR"] and report["marker"] is False

    def test_an_existing_unrestricted_singleton_makes_the_export_fail_instead_of_continuing(self):
        run = run_python(
            """
            from empiremuse.i18n.service import get_i18n
            get_i18n()
            from empiremuse.site_data import main
            raise SystemExit(main(["--bot-version", "dev"]))
            """
        )

        assert run.returncode == 2 and "outro conjunto de idiomas" in run.stderr


class TestCommandLine:
    def test_it_writes_the_file(self, tmp_path):
        out = tmp_path / "site-data.json"

        run = subprocess.run(
            [sys.executable, "-m", "empiremuse.site_data", "--bot-version", "2.3.0", "--out", str(out)],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        assert run.returncode == 0, run.stderr
        assert json.loads(out.read_text("utf-8"))["bot_version"] == "2.3.0"

    def test_an_invalid_version_fails_without_writing(self, tmp_path, capsys):
        out = tmp_path / "site-data.json"

        assert main(["--bot-version", "bad", "--out", str(out)]) == 2

        assert not out.exists() and "bot_version" in capsys.readouterr().err
