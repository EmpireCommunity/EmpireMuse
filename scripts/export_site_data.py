"""Gera o ``site-data.json`` (comandos públicos do bot para o site) sem rede e sem login no Discord.

Invólucro fino: a lógica fica em ``empiremuse/site_data.py`` (que a imagem Docker já leva, para o release rodar o exportador dentro dela).
Uso: ``python scripts/export_site_data.py --bot-version 2.3.0 --out site-data.json``.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from empiremuse.site_data import main  # noqa: E402

if __name__ == "__main__":
    raise SystemExit(main())
