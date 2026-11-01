# -*- coding: utf-8 -*-
"""Localização (item 21): ``pool.i18n``, o carregador dos catálogos e a CLI ``python -m empiremuse.i18n.check``."""

from empiremuse.i18n.loader import I18nError
from empiremuse.i18n.service import CatalogProvider, I18n, get_i18n, localized

__all__ = ["CatalogProvider", "I18n", "I18nError", "get_i18n", "localized"]
