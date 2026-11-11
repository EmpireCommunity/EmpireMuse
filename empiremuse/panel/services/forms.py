"""As categorias de configuração do servidor e os formulários de cada uma (compartilhado pelo painel genérico e pela API)."""

from __future__ import annotations

from empiremuse.panel.services.appearance import SkinsGlobalForm, SkinsServerForm
from empiremuse.panel.services.base import SettingsForm
from empiremuse.panel.services.general import LanguageForm, PrefixForm, VoiceStatusForm
from empiremuse.panel.services.links import LinksForm
from empiremuse.panel.services.listen_along import ListenAlongForm
from empiremuse.panel.services.player import DjForm, PlayerForm

CATEGORY_FORMS: dict[str, tuple[SettingsForm, ...]] = {
    "player": (PlayerForm(),),
    "dj": (DjForm(),),
    "aparencia": (SkinsServerForm(), SkinsGlobalForm()),
    "links": (LinksForm(),),
    "geral": (PrefixForm(), VoiceStatusForm(), LanguageForm(), ListenAlongForm()),
}
