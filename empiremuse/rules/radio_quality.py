"""A qualidade de uma estação ao vivo para o usuário escolher (R5): um pontinho colorido por estação, calculado só com o que já se sabe.

Os limites são nomeados (propostos pelo Turbine no PR; o dono ajusta aqui):

- 🔴 ``failing``: nossa própria saúde a marcou indisponível (3 ciclos esgotados em 1 h) OU a última checagem do Radio Browser falhou;
- 🟠 ``weak``: taxa abaixo de ``BITRATE_OK_KBPS`` (ou desconhecida);
- 🟡 ``ok``: taxa entre ``BITRATE_OK_KBPS`` e ``BITRATE_GOOD_KBPS``, ou boa mas sem a checagem do diretório ter confirmado;
- 🟢 ``good``: taxa de ``BITRATE_GOOD_KBPS`` para cima E a última checagem do diretório deu certo.
"""

from __future__ import annotations

GOOD, OK, WEAK, FAILING = "good", "ok", "weak", "failing"
DOTS = {GOOD: "🟢", OK: "🟡", WEAK: "🟠", FAILING: "🔴"}
BITRATE_GOOD_KBPS = 128
BITRATE_OK_KBPS = 64


def station_quality(bitrate_kbps: int, *, check_ok: bool | None, unavailable: bool) -> str:
    """``check_ok``: a última checagem do Radio Browser (``None`` = não se sabe); ``unavailable``: a nossa saúde marcou a estação."""
    if unavailable or check_ok is False:
        return FAILING
    if bitrate_kbps < BITRATE_OK_KBPS:
        return WEAK  # inclui 0: taxa desconhecida não é prova de qualidade
    if bitrate_kbps >= BITRATE_GOOD_KBPS and check_ok:
        return GOOD
    return OK


def quality_dot(quality: str) -> str:
    return DOTS.get(quality, DOTS[OK])
