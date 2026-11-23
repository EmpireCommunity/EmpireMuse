"""``COVER_HOSTS`` no ``config_loader``: vazio = a lista padrão, ``*`` aceito e valor inválido impede a partida (falha cedo)."""

import pytest

from empiremuse import config_loader
from empiremuse.rules.cover import DEFAULT_COVER_HOSTS, normalize_cover_hosts

pytestmark = pytest.mark.critical


@pytest.fixture
def home(monkeypatch, tmp_path):
    pasta, cwd = tmp_path / "home", tmp_path / "cwd"
    pasta.mkdir()
    cwd.mkdir()
    monkeypatch.setenv("EMPIREMUSE_HOME", str(pasta))
    monkeypatch.chdir(cwd)
    monkeypatch.delenv("COVER_HOSTS", raising=False)
    return pasta


@pytest.mark.parametrize(
    ("value", "expected"),
    [(None, normalize_cover_hosts(DEFAULT_COVER_HOSTS)), ("", normalize_cover_hosts(DEFAULT_COVER_HOSTS)),
     ("*", "*"), ("i.scdn.co", "i.scdn.co")],
)  # fmt: skip
def test_valid_values_load(home, monkeypatch, value, expected):
    if value is not None:
        monkeypatch.setenv("COVER_HOSTS", value)

    assert config_loader.load_config()["COVER_HOSTS"] == expected


@pytest.mark.parametrize("value", ["*.com", "i.scdn.co *", "https://i.scdn.co"])
def test_an_invalid_value_stops_the_boot_with_a_clear_error(home, monkeypatch, value):
    monkeypatch.setenv("COVER_HOSTS", value)

    with pytest.raises(Exception, match="COVER_HOSTS") as erro:
        config_loader.load_config()
    assert "configuração inválida" in str(erro.value)
