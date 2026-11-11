"""B7: ``site-data.json`` (comandos públicos para o site). O que importa: roda sem rede, bate com o schema, nunca vaza comando de dono,
oculto, id interno nem o nome do cog, e a saída é estável."""

import json
import re
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator

from empiremuse import site_data
from empiremuse.site_data import SiteDataError, build_site_data, is_public, main, render

pytestmark = pytest.mark.critical

SCHEMA = json.loads((Path(__file__).resolve().parents[2] / "docs" / "api" / "site-data.schema.json").read_text("utf-8"))
SNOWFLAKE = re.compile(r"\d{17,20}")
# Comandos de dono conhecidos (e os aliases deles): escritos à mão de propósito, para o teste não depender do filtro que ele vigia.
OWNER_IDS_AND_ALIASES = {
    "shell", "hardreset", "reload", "reloadconfig", "reloadskins", "panel", "cleardm", "getlavaservers", "restartlavalink",
    "setavatar", "setbanner", "setguildprefix", "update", "updatelavalink", "saveplayers", "servers", "userbans", "ytoauth",
    "premium", "pauseall", "resumeall", "pausartodas", "retomartodas", "radio.premium", "radio.pauseall", "radio.resumeall",
    "radio.bot", "help", "ajuda",
}  # fmt: skip


@pytest.fixture(scope="module")
def data():
    return build_site_data("2.3.0-rc.1")


def flatten(nodes):
    for node in nodes:
        yield node
        yield from flatten(node.get("subcommands", ()))


def every_command(data):
    return list(flatten([*data["slash_commands"], *data["prefix_commands"]]))


def command(data, kind, id_):
    return next(node for node in flatten(data[f"{kind}_commands"]) if node["id"] == id_)


class TestOffline:
    def test_builds_without_network_or_discord(self, monkeypatch):
        def denied(*_args, **_kwargs):
            raise AssertionError("o exportador não pode usar a rede")

        monkeypatch.setattr(socket.socket, "connect", denied)
        monkeypatch.setattr(socket, "getaddrinfo", denied)

        assert build_site_data("dev")["slash_commands"]


class TestShape:
    def test_it_matches_the_published_schema(self, data):
        Draft202012Validator.check_schema(SCHEMA)

        Draft202012Validator(SCHEMA).validate(data)

    def test_it_is_stable_and_has_no_dates(self, data):
        text = render(data)

        assert text == render(build_site_data("2.3.0-rc.1")) and text.endswith("}\n")
        assert not re.search(r"20\d\d-\d\d-\d\d", text)

    def test_the_catalog_content_is_there_in_both_languages(self, data):
        play = command(data, "slash", "play")
        assert {o["required"] for o in play["options"]} >= {True, False} and play["name"]["pt-BR"] != ""
        assert command(data, "slash", "queue.display")["path"] == ["queue", "display"]  # subcomando
        assert command(data, "slash", "language")["permissions"] == ["manage_guild"]
        assert command(data, "slash", "language")["category"]["label"] == {
            "pt-BR": "Configurações",
            "en-US": "Settings",
        }
        radio = command(data, "prefix", "radio")
        assert radio["aliases"] == {"pt-BR": ["rd", "247"], "en-US": ["rd", "247"]}
        assert command(data, "prefix", "now_playing")["path"] == ["nowplaying"]  # PREFIX_CATALOG_IDS
        assert command(data, "prefix", "setprefix")["permissions"] == ["manage_guild"]

    @pytest.mark.parametrize("version", ["2.3.0", "2.3.0-rc.1", "dev"])
    def test_a_release_version_is_accepted(self, version):
        assert build_site_data(version)["bot_version"] == version

    @pytest.mark.parametrize("version", ["", "v2.3.0", "2.3", "2.3.0-beta", "2.3.0\n", "x; rm -rf /"])
    def test_anything_else_is_refused(self, version):
        with pytest.raises(SiteDataError):
            build_site_data(version)


class TestNothingPrivateLeaks:
    @pytest.mark.parametrize(
        "command_like",
        [
            SimpleNamespace(hidden=True, checks=[]),
            SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__="admin_owner_only.<locals>.predicate")]),
            SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__="is_owner.<locals>.predicate")]),
            SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__="RadioPrefixCommands.owner_check")]),
        ],
    )
    def test_the_filter_drops_hidden_and_owner_commands(self, command_like):
        assert is_public(command_like) is False

    def test_a_plain_command_passes_the_filter(self):
        assert is_public(SimpleNamespace(hidden=False, checks=[SimpleNamespace(__qualname__="check_voice.<locals>.p")]))

    def test_no_known_owner_command_or_alias_is_exported(self, data):
        exported = {node["id"] for node in every_command(data)}
        for node in every_command(data):
            exported.update(node["path"])
            if "aliases" in node:
                exported.update(alias for aliases in node["aliases"].values() for alias in aliases)
                exported.add(".".join(node["path"]))

        assert exported & OWNER_IDS_AND_ALIASES == set()

    def test_every_hidden_or_owner_command_in_the_code_is_absent(self, data):
        """Recalcula, de forma independente do filtro, quem é de dono no código (oculto ou com a verificação de dono) e exige que fique de fora."""
        exported = {node["id"] for node in flatten(data["prefix_commands"])}
        private = []
        for cls in site_data._cog_classes():
            for cmd in getattr(cls, "__cog_commands__", []):
                stack = [cmd]
                while stack:
                    item = stack.pop()
                    stack.extend(getattr(item, "commands", ()))
                    owner = any("admin_owner_only" in c.__qualname__ for c in item.checks)
                    if item.hidden or owner:
                        private.append(site_data.catalog_id(item))
        assert private, "o teste perdeu de vista os comandos de dono do código"

        assert set(private) & exported == set()

    def test_no_internal_id_or_cog_name_in_the_output(self, data):
        text = render(data)

        assert not SNOWFLAKE.search(text)
        assert not any(key in text for key in ('"cog"', '"_cog"', '"_category"', "OWNER_IDS"))
        assert not {cls.__cog_name__ for cls in site_data._cog_classes() if cls.__cog_name__ == "Owner"} & set(
            re.findall(r'"(\w+)":', text)
        )


class TestOnlyTheCommittedCatalogs:
    def test_another_locale_directory_is_never_read_nor_exported(self, tmp_path, monkeypatch):
        """Um idioma extra na pasta de catálogos (como o que o Crowdin baixa) nem é aberto: o marcador dele não chega à saída."""
        import shutil

        from empiremuse.i18n import loader

        for locale in ("en-US", "pt-BR"):
            shutil.copytree(loader.DEFAULT_ROOT / locale, tmp_path / locale)
        shutil.copytree(loader.DEFAULT_ROOT / "en-US", tmp_path / "fr")
        for name in ("commands", "bot"):
            file = tmp_path / "fr" / f"{name}.json"
            file.write_text(
                file.read_text("utf-8").replace('"Show information about me."', '"MARCADOR-DO-CROWDIN"'), "utf-8"
            )
        monkeypatch.setattr(loader, "DEFAULT_ROOT", tmp_path)
        monkeypatch.setattr("empiremuse.i18n.service.loader.DEFAULT_ROOT", tmp_path)

        text = render(build_site_data("dev"))
        opened = []
        real = loader.read_catalog
        monkeypatch.setattr(loader, "read_catalog", lambda path: opened.append(path.parent.name) or real(path))
        build_site_data("dev")

        assert "MARCADOR-DO-CROWDIN" not in text and '"fr"' not in text
        assert set(opened) == {"en-US", "pt-BR"}  # nenhum outro diretório de idioma foi aberto

    def test_the_top_level_shape_is_exactly_the_documented_one(self, data):
        assert list(data) == ["schema_version", "bot_version", "locales", "slash_commands", "prefix_commands"]


class TestCommandLine:
    def test_it_writes_the_file(self, tmp_path):
        out = tmp_path / "site-data.json"

        assert main(["--bot-version", "2.3.0", "--out", str(out)]) == 0

        assert json.loads(out.read_text("utf-8"))["bot_version"] == "2.3.0"

    def test_an_invalid_version_fails_without_writing(self, tmp_path, capsys):
        out = tmp_path / "site-data.json"

        assert main(["--bot-version", "bad", "--out", str(out)]) == 2

        assert not out.exists() and "bot_version" in capsys.readouterr().err
