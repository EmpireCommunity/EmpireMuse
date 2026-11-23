"""Skin de imagem ``muse_slim_black`` (spec §0.1): o texto do embed e os componentes em ``_card.ImageSkin``; o desenho é do pipeline."""

from empiremuse.utils.music.skins.muse._card import ImageSkin


def load() -> ImageSkin:
    return ImageSkin("muse_slim_black")
