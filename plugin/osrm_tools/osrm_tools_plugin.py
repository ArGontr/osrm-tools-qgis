# -*- coding: utf-8 -*-
from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon

from .processing.provider import OSRMProvider
import os

ICON_PATH = os.path.join(os.path.dirname(__file__), 'icon.svg')


class OSRMToolsPlugin:
    """Registra el provider de Processing de OSRM Tools."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None

    def initGui(self):  # noqa: N802 (API de QGIS)
        self.provider = OSRMProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)

    def unload(self):
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    @staticmethod
    def icon():
        return QIcon(ICON_PATH)
