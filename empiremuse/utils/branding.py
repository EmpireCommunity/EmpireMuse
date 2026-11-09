"""Identidade centralizada do EmpireMuse (nome, URLs, assets e créditos).

Obra derivada de MuseHeart-MusicBot (zRitsu), licenciada sob GPL-2.0: os créditos
ao projeto original são preservados em ``UPSTREAM_*``, em ``CREDITS.md`` e em ``LICENSE``.
"""

from functools import lru_cache

from empiremuse.paths import PROJECT_DIR
from empiremuse.rules.release import Version
from empiremuse.version import image_version

BOT_NAME = "EmpireMuse"
GITHUB_REPO = "EmpireCommunity/EmpireMuse"
DOCKER_IMAGE_REPO = "or4cu1o/empiremuse"
REPO_URL = f"https://github.com/{GITHUB_REPO}"
SOURCE_REPO_GIT = REPO_URL + ".git"
RAW_BASE_URL = "https://raw.githubusercontent.com/EmpireCommunity/EmpireMuse/main"
LICENSE_URL = REPO_URL + "/blob/main/LICENSE"
GUIDES_URL = REPO_URL + "/blob/main/docs/guides"
RPC_APP_URL = "https://github.com/EmpireCommunity/EmpireMuse-RPC-app"
RPC_APP_RELEASES_URL = RPC_APP_URL + "/releases"
LAVALINK_BINARIES_URL = "https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/download/0.0.5"
LAVALINK_JAR_URL = LAVALINK_BINARIES_URL + "/Lavalink.jar"
LAVALINK_APPLICATION_YML_URL = LAVALINK_BINARIES_URL + "/application.yml"
# SHA-256 fixos dos binários da release 0.0.5 (conferem com o SHA256SUMS publicado); lidos em tempo de chamada.
LAVALINK_JAR_SHA256 = "8cb801e591072c3689fafd71ccf571a95a4ead3cc35dfc045e157d763d89119a"
LAVALINK_APPLICATION_YML_SHA256 = "39ca3e7813e0db338535b0e098dd084ea22c278bb74a8ddeac92f1fc65f8bb46"
# URLs do Lavalink.jar de versões oficiais do espelho (inclusive sob o nome antigo do repositório). Quem as mantém no
# .env (copiado de um .example.env antigo) é atualizado sozinho para a URL e o hash atuais; ver prepare_lavalink_files.
LAVALINK_JAR_OFFICIAL_URLS = frozenset(
    {
        "https://github.com/DiscordEmpireHub/LL-binaries/releases/download/0.0.1/Lavalink.jar",
        "https://github.com/DiscordEmpireHub/LL-binaries/releases/download/0.0.2/Lavalink.jar",
        "https://github.com/DiscordEmpireHub/EmpireMuse-LL-binaries/releases/download/0.0.1/Lavalink.jar",
        "https://github.com/DiscordEmpireHub/EmpireMuse-LL-binaries/releases/download/0.0.2/Lavalink.jar",
        "https://github.com/DiscordEmpireHub/EmpireMuse-LL-binaries/releases/download/0.0.3/Lavalink.jar",
        "https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/download/0.0.1/Lavalink.jar",
        "https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/download/0.0.2/Lavalink.jar",
        "https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/download/0.0.3/Lavalink.jar",
        "https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/download/0.0.4/Lavalink.jar",
        "https://github.com/EmpireCommunity/EmpireMuse-LL-binaries/releases/download/0.0.5/Lavalink.jar",
    }
)
# Versões oficiais do application.yml que o bot pode substituir (a 0.0.1, a 0.0.2, a 0.0.3 e a atual); qualquer outra é do dono.
LAVALINK_APPLICATION_YML_KNOWN_SHA256 = frozenset(
    {
        "f93ce9789fb637dc20bd886857a4ed813ad80870b43d268bc0ce80c11f90bfc3",
        "0d17d65f3b7d7a889f2b23f2719bd8a1e1aac77b90e0b77e9dacdbd35ce94801",
        "a342d5575fac9c394faaaecaae17df1c8fc759dc8a8937ff839aeabb07cb6b95",
        LAVALINK_APPLICATION_YML_SHA256,
    }
)
# JDK baixado quando não há Java 17+ no sistema: plataforma -> (URL oficial da BellSoft, SHA-256 fixo do arquivo).
# x64: Liberica 25 LTS (JDK zip no Windows, JDK lite tar.gz no Linux). O JDK 25 não tem build x86 de 32 bits (JEP 503), então
# as chaves i586 ficam no último Liberica 21.0.x que as publica.
# Para atualizar: baixe o arquivo novo de download.bell-sw.com e confira por três canais: o SHA-256 baixado igual ao `digest` do
# asset em bell-sw/Liberica (GitHub Releases), e o SHA-1 baixado igual ao da api.bell-sw.com. Só então grave o SHA-256.
JDK_DOWNLOADS = {
    "windows-amd64": (
        "https://download.bell-sw.com/java/25.0.5.0.3+5/bellsoft-jdk25.0.5.0.3+5-windows-amd64.zip",
        "d7c90823ea2a24778d7499a82379b5e18c7ce3f4cc7a9d9c9af5b133abe148f0",
    ),
    "windows-i586": (
        "https://download.bell-sw.com/java/21.0.13.0.3+5/bellsoft-jdk21.0.13.0.3+5-windows-i586.zip",
        "fc643ea5aa3fde487ee020c412cb5919acae98af16e8ec3b0965ff5149c1a9ae",
    ),
    "linux-amd64": (
        "https://download.bell-sw.com/java/25.0.5.0.3+5/bellsoft-jdk25.0.5.0.3+5-linux-amd64-lite.tar.gz",
        "15943cdc94632bd2f299b17e6b9602e5de99631122529bab242f5514ddc00249",
    ),
    "linux-i586": (
        "https://download.bell-sw.com/java/21.0.13.0.3+5/bellsoft-jdk21.0.13.0.3+5-linux-i586-lite.tar.gz",
        "c9e555bd50f68c4626b4902100c4e5af668461e94940785502990e7eba21dd75",
    ),
}
# Plugins do Lavalink (nome canônico em plugins/ -> SHA-256); a URL é LAVALINK_BINARIES_URL + "/" + nome.
LAVALINK_PLUGIN_SHA256 = {
    "youtube-plugin-2be8e542d3f6f178e048dca565892684c2e40177.jar": "b062effdda6abeaa46913dfd1b5d5600120b65f239ce88f30948c7213a3ca1c0",
    "lavasrc-plugin-4.8.3.jar": "4ec389bdae64fd381ee62fcdb902e15d9402823b1ad0b982bd5633619a07c1b3",
    "lavalink-2.7.1.jar": "4d63a711fa43f0f8ee88006e63b03d24c571ff54ea73d25cba83453b172cbf51",
    "soundy-plugin-1.1.0.jar": "9568b5930969346b1afba89df8c0c6d373c2b553e087af09fb3c190f252a3cc9",
}
LAVALINK_SERVER_LIST_URL = RAW_BASE_URL + "/data/lavalink_servers.ini"
SUPPORT_SERVER_URL = "https://empirecommunity.org/discord"
EMBED_COLOR_HEX = "8B5CF6"  # violeta da paleta Nebulosa
UPSTREAM_NAME = "MuseHeart-MusicBot"
UPSTREAM_AUTHOR = "zRitsu"
UPSTREAM_URL = "https://github.com/zRitsu/MuseHeart-MusicBot"

_ASSETS_DIR = PROJECT_DIR / "assets"
_UNSET = object()


def assets_base_url(version: Version | None | object = _UNSET) -> str:
    """URL base dos assets: a TAG da versão quando ela é conhecida, senão a ``main``.

    Com a tag (``v2.1.1``, ``v2.1.1-rc.1``) uma versão publicada nunca perde as próprias imagens, mesmo que a ``main`` mude de novo.
    Sem versão (checkout git, ``dev``, testes) vale a ``main``, como sempre valeu. Sem argumento usa a versão da imagem em execução.
    """
    if version is _UNSET:
        version = image_version()
    ref = f"v{version}" if version is not None else "main"
    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{ref}/assets"


# Calculada uma vez por processo; a versão da imagem não muda enquanto o bot roda.
ASSETS_BASE_URL = assets_base_url(image_version())


@lru_cache(maxsize=None)
def _assets_index() -> dict[str, str]:
    """``nome do arquivo -> subcaminho`` dos arquivos REAIS em ``assets/<subpasta>/``, montado uma vez.

    A raiz de ``assets/`` fica de fora (cópias de compatibilidade da 2.1.0 e o ``SHA256SUMS``). Nenhum link simbólico entra, nem de
    arquivo nem de pasta, aponte para onde apontar: o raw do GitHub serve o TEXTO do link, não a imagem, e um atalho de pasta criaria
    um segundo caminho para o mesmo arquivo. Basename repetido entre subpastas é erro: o nome deixa de apontar para um só arquivo, e o
    índice inteiro não é montado (só chamadas bem-sucedidas entram no cache).
    """
    root = _ASSETS_DIR.resolve()
    index: dict[str, str] = {}
    if not root.is_dir():
        return index
    for folder in sorted(p for p in _ASSETS_DIR.iterdir() if p.is_dir() and not p.is_symlink()):
        for path in sorted(folder.rglob("*")):
            if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(root):
                continue
            if path.name in index:
                raise ValueError(f"Nome de asset repetido em assets/: {path.name!r}")
            index[path.name] = path.relative_to(_ASSETS_DIR).as_posix()
    return index


def asset_url(name: str) -> str:
    """Retorna a URL pública de um arquivo em ``assets/<subpasta>/``, pelo nome do arquivo.

    Levanta ``ValueError`` se ``name`` for vazio, contiver separador de caminho (``/`` ou ``\\``) ou ``..``, se o nome não existir
    em nenhuma subpasta de ``assets/`` ou se o mesmo nome existir em mais de uma.
    """
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ValueError(f"Nome de asset inválido: {name!r}")
    subpath = _assets_index().get(name)
    if subpath is None:
        raise ValueError(f"Asset inexistente em assets/: {name!r}")
    return f"{ASSETS_BASE_URL}/{subpath}"
