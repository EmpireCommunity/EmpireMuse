"""Regras puras do filtro de local em cascata das rádios (item 8, R4): região, país, estado e cidade.

Aqui só há decisões e dados sem rede nem arquivo: a normalização de texto de estado (a MESMA no gerador e em execução), o país do servidor
a partir do idioma (o atalho ⭐), o formato dos ids dos nós (que vêm do usuário e só valem se existirem na árvore carregada) e os limites
da busca por região. A árvore em si é do ``utils/radio/locations`` e os dados são fixos (GeoNames, CC BY 4.0), nunca buscados em execução.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Container, Mapping
from typing import NamedTuple

MAX_OPTIONS = 24  # teto de qualquer nível (o select do Discord tem 25 e a 25ª é o ◀ Voltar / ⭐ do topo)
MAX_HITS = 150  # estações de uma escolha de local
RADIUS_M = 100_000  # o raio da cidade, em metros (``geo_distance`` do Radio Browser)
CITY_SPACING_KM = (
    25.0  # duas cidades da lista ficam a pelo menos isto uma da outra (o raio de 100 km já cobre as vizinhas)
)
MAX_STATE_TEXT = 128  # texto de estado maior que isto não é resolvido
MAX_TERM = 64  # termo de estado/cidade que vai ao Radio Browser
MAX_ID_LEN = 99
MAX_CLICKS = 10**9
FINAL_COUNTRY_STATIONS = 50  # país com menos estações que isto é escolha final (pula o estado)

AREAS = (
    "america_norte",
    "america_latina",
    "europa_ocidental_norte",
    "europa_oriental_sul",
    "oriente_medio_africa",
    "asia_pacifico",
)
# Sub-regiões M49 (CLDR) de cada região do topo. A única exceção à regra é o IR, que sai da Ásia e vai para Oriente Médio e África.
AREA_SUBREGIONS: Mapping[str, tuple[str, ...]] = {
    "america_norte": ("021",),
    "america_latina": ("005", "013", "029"),
    "europa_ocidental_norte": ("155", "154"),
    "europa_oriental_sul": ("151", "039"),
    "oriente_medio_africa": ("145", "015", "014", "011", "018", "017"),
    "asia_pacifico": ("030", "035", "034", "143", "053", "054", "057", "061", "QO"),
}
AREA_OVERRIDES: Mapping[str, str] = {"IR": "oriente_medio_africa"}

# Palavras que o texto de estado do Radio Browser traz e que não ajudam a achar a subdivisão.
NOISE_WORDS = ("brasil", "state", "estado", "province")

_NOT_ALNUM = re.compile(
    r"[\W_]+"
)  # letras e números de QUALQUER escrita (Москва, 北京): só o ASCII apagaria o nome inteiro
_SPLIT = re.compile(r"[,\-/()|;]")
_COUNTRY = re.compile(r"[A-Z]{2}")
_LOCALE = re.compile(r"[A-Za-z]{2,3}(?:[-_][A-Za-z0-9]{2,8}){0,3}")
_AREA_ID = re.compile(r"r:([a-z_]{1,40})")
_COUNTRY_ID = re.compile(r"c:([A-Z]{2})")
_STATE_ID = re.compile(r"s:([A-Z]{2}):(@?[A-Za-z0-9_.]{1,32})")
_CITY_ID = re.compile(r"x:([0-9]{1,12})")


def norm(text: object) -> str:
    """NFKD sem diacríticos, ``casefold`` e tudo que não é letra ou número (de qualquer escrita) vira um espaço; vazio se não for texto."""
    if not isinstance(text, str):
        return ""
    decomposed = unicodedata.normalize("NFKD", text)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char)).casefold()
    return _NOT_ALNUM.sub(" ", plain).strip()


class StateIndex(NamedTuple):
    """O que o resolvedor precisa de um país: ``aliases[cc][texto normalizado] = admin1``, ``cities[cc][nome normalizado] = admin1`` (só nomes
    que existem em UMA subdivisão) e ``names[cc]`` com o nome normalizado do país (para tirá-lo do texto)."""

    aliases: Mapping[str, Mapping[str, str]]
    cities: Mapping[str, Mapping[str, str]]
    names: Mapping[str, str]


def _clean_piece(piece: str, country_name: str) -> str:
    """Tira do texto já normalizado o nome do país e as palavras de ruído, sem esvaziá-lo."""
    words = [country_name, *NOISE_WORDS] if country_name else list(NOISE_WORDS)
    result = piece
    for word in words:
        if word and result != word:
            result = re.sub(rf"\b{re.escape(word)}\b", " ", result)
    return re.sub(r"\s+", " ", result).strip() or piece


def _variants(piece: str, country_name: str) -> tuple[str, ...]:
    """O pedaço como está e sem o nome do país/ruído: "Western Australia" é um estado inteiro, "Goiás Brazil" é Goiás."""
    cleaned = _clean_piece(piece, country_name)
    return (piece, cleaned) if cleaned != piece else (piece,)


def resolve_state(index: StateIndex, cc: object, text: object) -> str | None:
    """O ``admin1`` do GeoNames a que o texto de estado do Radio Browser se refere, ou ``None``. Nunca levanta, nunca devolve o texto.

    Ordem: (1) o texto inteiro é um apelido exato (nome GeoNames, nome ISO, sigla ou apelido manual), com e sem o nome do país; (2) qualquer pedaço
    separado por ``, - / ( ) | ;``, do último para o primeiro; (3) a última palavra de 2 ou 3 letras como sigla ("Sydney NSW", "Boston MA"); (4) o nome de
    uma cidade que só existe numa subdivisão do país.
    """
    if not isinstance(cc, str) or not isinstance(text, str) or not text.strip() or len(text) > MAX_STATE_TEXT:
        return None
    aliases = index.aliases.get(cc, {})
    country_name = index.names.get(cc, "")
    whole = norm(text)
    if not whole:
        return None
    for candidate in _variants(whole, country_name):
        if (found := aliases.get(candidate)) is not None:
            return found
    pieces = [piece for piece in (norm(p) for p in _SPLIT.split(text)) if piece]
    for piece in reversed(pieces):
        for candidate in _variants(piece, country_name):
            if (found := aliases.get(candidate)) is not None:
                return found
    last = whole.rsplit(" ", 1)[-1]
    if " " in whole and 2 <= len(last) <= 3 and (found := aliases.get(last)) is not None:
        return found
    cities = index.cities.get(cc, {})
    for piece in (_clean_piece(whole, country_name), *reversed(pieces)):
        if (found := cities.get(piece)) is not None:
            return found
    return None


# ---------------------------------------------------------------------- país do servidor (o atalho ⭐)


def guild_country(locale: object, available: Container[str]) -> str | None:
    """O país do idioma do servidor se ele está na árvore, senão ``None``.

    Usa o território do locale (``pt-BR`` → BR) e, sem ele, os ``likely_subtags`` do CLDR pelo Babel (``fr`` → FR, ``ja`` → JP, ``uk`` → UA).
    ``es-419`` (419 não é país), idioma desconhecido (o Babel levanta ``UnknownLocaleError`` com ``no``) e país fora da árvore dão ``None``.
    """
    if not isinstance(locale, str) or _LOCALE.fullmatch(locale) is None:
        return None
    from babel import Locale, UnknownLocaleError
    from babel.core import get_global

    try:
        parsed = Locale.parse(locale.replace("-", "_"))
    except ValueError, UnknownLocaleError, TypeError:
        return None
    territory = parsed.territory
    if territory is None:
        likely = get_global("likely_subtags").get(parsed.language)
        territory = likely.rsplit("_", 1)[-1] if isinstance(likely, str) else None
    if not isinstance(territory, str) or _COUNTRY.fullmatch(territory) is None or territory not in available:
        return None
    return territory


# ---------------------------------------------------------------------- ids dos nós (vêm do usuário: só valem se a árvore os conhece)


class NodeId(NamedTuple):
    kind: str  # region | country | state | city
    cc: str | None
    code: str  # slug da região, país, código do estado/grupo ou geonameid


def parse_node_id(raw: object) -> NodeId | None:
    """Decompõe um id de nó pelo formato. Quem chama AINDA confere que ele existe na árvore carregada."""
    if not isinstance(raw, str) or not raw or len(raw) > MAX_ID_LEN:
        return None
    if match := _AREA_ID.fullmatch(raw):
        return NodeId("region", None, match.group(1)) if match.group(1) in AREAS else None
    if match := _COUNTRY_ID.fullmatch(raw):
        return NodeId("country", match.group(1), match.group(1))
    if match := _STATE_ID.fullmatch(raw):
        return NodeId("state", match.group(1), match.group(2))
    if match := _CITY_ID.fullmatch(raw):
        return NodeId("city", None, match.group(1))
    return None


def country_id(cc: str) -> str:
    return f"c:{cc}"


def state_id(cc: str, code: str) -> str:
    return f"s:{cc}:{code}"


def city_id(geoname_id: int) -> str:
    return f"x:{geoname_id}"


def area_id(slug: str) -> str:
    return f"r:{slug}"


# ---------------------------------------------------------------------- números e coordenadas vindos do diretório (hostil)


def valid_coordinates(lat: object, lon: object) -> bool:
    """Latitude e longitude finitas e dentro da faixa (``bool`` e texto não valem)."""
    if (
        isinstance(lat, bool)
        or isinstance(lon, bool)
        or not isinstance(lat, int | float)
        or not isinstance(lon, int | float)
    ):
        return False
    return math.isfinite(lat) and math.isfinite(lon) and -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0


def clamp_clicks(value: object) -> int:
    """``clickcount`` do diretório limitado a 0..10⁹; o que não é número vira 0."""
    if isinstance(value, bool):
        return 0
    if (
        isinstance(value, str) and value.strip().isdecimal() and len(value.strip()) <= 12
    ):  # sem ``int()`` de texto gigante (ValueError)
        value = int(value.strip())
    if not isinstance(value, int | float) or not math.isfinite(value):
        return 0
    return max(0, min(MAX_CLICKS, int(value)))


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Distância (haversine) em km entre dois pontos."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    a = (
        math.sin((phi2 - phi1) / 2) ** 2
        + math.cos(phi1) * math.cos(phi2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2
    )
    return 6371.0088 * 2 * math.asin(min(1.0, math.sqrt(a)))


def search_term(raw: object) -> str | None:
    """O termo de estado ou cidade que vai ao Radio Browser: sem controle, 2 a 64 caracteres; senão ``None`` (a URL é montada pelo ``yarl``)."""
    if not isinstance(raw, str):
        return None
    term = raw.strip()
    if not 2 <= len(term) <= MAX_TERM or any(ord(char) < 32 or ord(char) == 127 for char in term):
        return None
    return term
