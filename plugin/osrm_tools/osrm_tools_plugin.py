# -*- coding: utf-8 -*-
from qgis.core import QgsApplication
from qgis.PyQt.QtGui import QIcon

from .processing.provider import OSRMProvider
from .server.controller import LocalServerController
import os

ICON_PATH = os.path.join(os.path.dirname(__file__), 'icon.svg')


class OSRMToolsPlugin:
    """Registra el provider de Processing de OSRM Tools."""

    def __init__(self, iface):
        self.iface = iface
        self.provider = None
        self.server_controller = None

    def initGui(self):  # noqa: N802 (API de QGIS)
        self.provider = OSRMProvider()
        QgsApplication.processingRegistry().addProvider(self.provider)
        self.server_controller = LocalServerController(self.iface, ICON_PATH)
        self.server_controller.schedule_auto_start()

    def unload(self):
        if self.server_controller is not None:
            self.server_controller.shutdown()
            self.server_controller.remove_action()
            self.server_controller = None
        if self.provider is not None:
            QgsApplication.processingRegistry().removeProvider(self.provider)
            self.provider = None

    @staticmethod
    def icon():
        return QIcon(ICON_PATH)
