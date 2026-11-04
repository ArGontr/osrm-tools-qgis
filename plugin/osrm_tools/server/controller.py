# -*- coding: utf-8 -*-
"""Integración del servidor local con el ciclo de vida del plugin."""
from pathlib import Path

from qgis.core import QgsApplication, QgsMessageLog
from qgis.PyQt.QtCore import Qt, QTimer
from qgis.PyQt.QtGui import QIcon
try:
    from qgis.PyQt.QtGui import QAction  # Qt6 / QGIS 4
except ImportError:
    from qgis.PyQt.QtWidgets import QAction  # Qt5 / QGIS 3
from qgis.PyQt.QtWidgets import QMessageBox

from .dialog import ServerDialog
from .manager import LocalServerManager, is_osrm_running

MENU_NAME = '&OSRM Tools'


class LocalServerController:
    def __init__(self, iface, icon_path):
        self.iface = iface
        base_dir = Path(QgsApplication.qgisSettingsDirPath()) / 'osrm_tools'
        self.manager = LocalServerManager(base_dir, logger=self._log)
        self.dialog = None
        self.closed = False
        self.action = QAction(QIcon(str(icon_path)), 'Servidor OSRM local…', iface.mainWindow())
        self.action.triggered.connect(self.show_dialog)
        self.iface.addPluginToMenu(MENU_NAME, self.action)
        self.iface.addToolBarIcon(self.action)
        QgsApplication.instance().aboutToQuit.connect(self.shutdown)

    @staticmethod
    def _log(message):
        if message.strip():
            QgsMessageLog.logMessage(message, 'OSRM Tools')

    def auto_start(self):
        if self.closed:
            return
        self.manager.adopt_owned_container()
        if is_osrm_running():
            return
        answer = QMessageBox.question(
            self.iface.mainWindow(), 'Servidor OSRM local',
            'No se detectó un servidor OSRM en http://127.0.0.1:5000.\n\n'
            '¿Deseas configurar e iniciar uno ahora? Podrás usar Docker o '
            'binarios locales y elegir un PBF propio o de Geofabrik.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No)
        if answer == QMessageBox.StandardButton.Yes:
            self.show_dialog()

    def schedule_auto_start(self):
        # Deja terminar initGui y evita bloquear la carga completa de QGIS.
        QTimer.singleShot(750, self.auto_start)

    def show_dialog(self):
        if self.closed:
            return
        if self.dialog is None:
            self.dialog = ServerDialog(self.manager, self.iface.mainWindow())
            self.dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
            self.dialog.destroyed.connect(self._dialog_destroyed)
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def _dialog_destroyed(self):
        self.dialog = None

    def remove_action(self):
        self.iface.removePluginMenu(MENU_NAME, self.action)
        self.iface.removeToolBarIcon(self.action)
        self.action.deleteLater()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        if self.dialog is not None:
            self.dialog.shutdown()
            self.dialog.close()
            self.dialog = None
        else:
            self.manager.stop()
