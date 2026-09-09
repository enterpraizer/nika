"""Модуль агентов актуализации сведений об операционных системах (ЛР4)."""

from sc_kpm import ScModule

from .OsSupportAgent import OsSupportAgent


class OsSupportModule(ScModule):
    def __init__(self):
        super().__init__(OsSupportAgent())
