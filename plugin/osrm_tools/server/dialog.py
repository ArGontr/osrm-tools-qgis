# -*- coding: utf-8 -*-
"""Diálogo Qt para configurar y controlar el servidor local."""
import threading
from pathlib import Path

from qgis.core import QgsSettings
from qgis.PyQt.QtCore import QThread, QUrl, pyqtSignal
from qgis.PyQt.QtGui import QDesktopServices
from qgis.PyQt.QtWidgets import (
    QComboBox, QDialog, QFileDialog, QFormLayout, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QProgressBar, QPushButton,
    QRadioButton, QStackedWidget, QVBoxLayout, QWidget,
)

from .manager import DOCKER_IMAGE, OperationCancelled, is_osrm_running

PREFIX = 'osrm_tools/server/'
DEFAULT_GEOFABRIK = 'https://download.geofabrik.de/north-america/mexico-latest.osm.pbf'
DOCKER_DOWNLOAD = 'https://www.docker.com/products/docker-desktop/'


class ServerWorker(QThread):
    progressed = pyqtSignal(int, str)
    logged = pyqtSignal(str)
    succeeded = pyqtSignal(dict)
    failed = pyqtSignal(str)

    def __init__(self, manager, config, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.config = config
        self.cancel_event = threading.Event()

    def cancel(self):
        self.cancel_event.set()

    def run(self):
        previous_logger = self.manager.logger
        self.manager.logger = self.logged.emit
        try:
            result = self.manager.prepare_and_start(
                self.config, self.progressed.emit, self.cancel_event)
            self.succeeded.emit(result)
        except OperationCancelled as exc:
            self.failed.emit(str(exc))
        except Exception as exc:  # noqa: BLE001: el hilo no debe cruzar excepciones
            self.failed.emit(str(exc))
        finally:
            self.manager.logger = previous_logger


class ServerDialog(QDialog):
    def __init__(self, manager, parent=None):
        super().__init__(parent)
        self.manager = manager
        self.worker = None
        self.last_result = None
        self.setWindowTitle('Servidor OSRM local')
        self.setMinimumWidth(650)
        self._build_ui()
        self._load_settings()
        self._refresh_status()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        intro = QLabel(
            'Prepara un grafo de auto y deja OSRM disponible para los algoritmos en '
            '<b>http://127.0.0.1:5000</b>. El grafo se reutiliza mientras el PBF no cambie.')
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.status_label = QLabel()
        self.status_label.setWordWrap(True)
        layout.addWidget(self.status_label)

        form = QFormLayout()
        self.backend_combo = QComboBox()
        self.backend_combo.addItem('Docker (recomendado)', 'docker')
        self.backend_combo.addItem('Binarios OSRM (alternativa; SHA-256)', 'native')
        form.addRow('Motor:', self.backend_combo)
        docker_button = QPushButton('Descargar Docker Desktop…')
        docker_button.clicked.connect(lambda: QDesktopServices.openUrl(QUrl(DOCKER_DOWNLOAD)))
        form.addRow('', docker_button)
        layout.addLayout(form)

        self.local_radio = QRadioButton('Usar un archivo .osm.pbf local')
        self.url_radio = QRadioButton('Descargar desde Geofabrik')
        self.local_radio.setChecked(True)
        layout.addWidget(self.local_radio)
        layout.addWidget(self.url_radio)
        self.source_stack = QStackedWidget()
        local_page = QWidget()
        local_layout = QHBoxLayout(local_page)
        local_layout.setContentsMargins(0, 0, 0, 0)
        self.pbf_edit = QLineEdit()
        self.pbf_edit.setPlaceholderText('/ruta/al/archivo.osm.pbf')
        browse_pbf = QPushButton('Examinar…')
        browse_pbf.clicked.connect(self._browse_pbf)
        local_layout.addWidget(self.pbf_edit, 1)
        local_layout.addWidget(browse_pbf)
        self.source_stack.addWidget(local_page)
        url_page = QWidget()
        url_layout = QHBoxLayout(url_page)
        url_layout.setContentsMargins(0, 0, 0, 0)
        self.url_edit = QLineEdit()
        self.url_edit.setPlaceholderText(DEFAULT_GEOFABRIK)
        url_layout.addWidget(self.url_edit)
        self.source_stack.addWidget(url_page)
        layout.addWidget(self.source_stack)
        self.local_radio.toggled.connect(self._update_source)
        self.url_radio.toggled.connect(self._update_source)

        data_layout = QHBoxLayout()
        data_layout.addWidget(QLabel('Carpeta de datos:'))
        self.data_edit = QLineEdit()
        browse_data = QPushButton('Examinar…')
        browse_data.clicked.connect(self._browse_data)
        data_layout.addWidget(self.data_edit, 1)
        data_layout.addWidget(browse_data)
        layout.addLayout(data_layout)

        self.progress_bar = QProgressBar()
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        layout.addWidget(self.progress_bar)
        self.log_edit = QPlainTextEdit()
        self.log_edit.setReadOnly(True)
        self.log_edit.setMaximumBlockCount(1500)
        self.log_edit.setMinimumHeight(150)
        layout.addWidget(self.log_edit, 1)

        buttons = QHBoxLayout()
        self.start_button = QPushButton('Preparar e iniciar')
        self.start_button.setDefault(True)
        self.start_button.clicked.connect(self.start_server)
        self.stop_button = QPushButton('Detener')
        self.stop_button.clicked.connect(self.stop_server)
        self.cancel_button = QPushButton('Cancelar operación')
        self.cancel_button.clicked.connect(self.cancel_operation)
        close_button = QPushButton('Cerrar')
        close_button.clicked.connect(self.close)
        buttons.addWidget(self.start_button)
        buttons.addWidget(self.stop_button)
        buttons.addWidget(self.cancel_button)
        buttons.addStretch(1)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def _load_settings(self):
        settings = QgsSettings()
        backend = settings.value(PREFIX + 'backend', 'docker', type=str)
        migration_key = PREFIX + 'docker_default_v1'
        if not settings.value(migration_key, False, type=bool):
            # Migra una sola vez instalaciones que guardaron "native" en versiones previas.
            if self.manager.docker_executable():
                backend = 'docker'
            settings.setValue(migration_key, True)
        index = self.backend_combo.findData(backend)
        self.backend_combo.setCurrentIndex(max(0, index))
        self.pbf_edit.setText(settings.value(PREFIX + 'pbf_path', '', type=str))
        self.url_edit.setText(settings.value(PREFIX + 'pbf_url', DEFAULT_GEOFABRIK, type=str))
        use_url = settings.value(PREFIX + 'use_url', False, type=bool)
        self.url_radio.setChecked(use_url)
        self.local_radio.setChecked(not use_url)
        default_data = str(Path(self.manager.base_dir) / 'data')
        self.data_edit.setText(settings.value(PREFIX + 'data_dir', default_data, type=str))
        self._update_source()

    def _save_settings(self):
        config = self.config()
        settings = QgsSettings()
        settings.setValue(PREFIX + 'backend', config['backend'])
        settings.setValue(PREFIX + 'pbf_path', config['pbf_path'])
        settings.setValue(PREFIX + 'pbf_url', config['pbf_url'])
        settings.setValue(PREFIX + 'use_url', bool(config['pbf_url']))
        settings.setValue(PREFIX + 'data_dir', config['data_dir'])
        settings.setValue(PREFIX + 'docker_image', DOCKER_IMAGE)

    def config(self):
        return {
            'backend': self.backend_combo.currentData(),
            'pbf_path': self.pbf_edit.text().strip(),
            'pbf_url': self.url_edit.text().strip() if self.url_radio.isChecked() else '',
            'data_dir': self.data_edit.text().strip(),
            'docker_image': DOCKER_IMAGE,
            'port': 5000,
        }

    def _update_source(self):
        self.source_stack.setCurrentIndex(1 if self.url_radio.isChecked() else 0)

    def _browse_pbf(self):
        path, _ = QFileDialog.getOpenFileName(
            self, 'Archivo OpenStreetMap', self.pbf_edit.text(),
            'OpenStreetMap PBF (*.osm.pbf);;Todos los archivos (*)')
        if path:
            self.pbf_edit.setText(path)
            self.local_radio.setChecked(True)

    def _browse_data(self):
        path = QFileDialog.getExistingDirectory(
            self, 'Carpeta para grafos y descargas', self.data_edit.text())
        if path:
            self.data_edit.setText(path)

    def _refresh_status(self):
        running = is_osrm_running()
        owned = self.manager.owns_server()
        if running and not owned:
            owned = self.manager.adopt_owned_container()
        if running:
            text = ('Hay un servidor OSRM activo en http://127.0.0.1:5000. '
                    + ('Fue creado por el plugin y se cerrará con QGIS.' if owned
                       else 'Es externo; el plugin no lo detendrá.'))
            self.status_label.setText(text)
        else:
            self.status_label.setText('No hay un servidor OSRM activo. Configura las opciones y pulsa Iniciar.')
        self.start_button.setEnabled(not running)
        self.stop_button.setEnabled(owned)
        self.cancel_button.setEnabled(False)

    def _set_busy(self, busy):
        self.start_button.setEnabled(not busy)
        self.stop_button.setEnabled(False)
        self.cancel_button.setEnabled(busy)
        self.backend_combo.setEnabled(not busy)
        self.pbf_edit.setEnabled(not busy)
        self.url_edit.setEnabled(not busy)
        self.data_edit.setEnabled(not busy)
        self.local_radio.setEnabled(not busy)
        self.url_radio.setEnabled(not busy)

    def start_server(self):
        config = self.config()
        if not config['data_dir']:
            QMessageBox.warning(self, 'OSRM local', 'Selecciona una carpeta de datos.')
            return
        if not config['pbf_url'] and not config['pbf_path']:
            QMessageBox.warning(self, 'OSRM local', 'Selecciona un PBF o una URL de Geofabrik.')
            return
        self._save_settings()
        self.last_result = None
        self.log_edit.clear()
        self._set_busy(True)
        self.progress_bar.setRange(0, 0)
        self.worker = ServerWorker(self.manager, config, self)
        self.worker.progressed.connect(self._on_progress)
        self.worker.logged.connect(self._append_log)
        self.worker.succeeded.connect(self._on_success)
        self.worker.failed.connect(self._on_failure)
        self.worker.finished.connect(self._on_thread_finished)
        self.worker.start()

    def _on_progress(self, value, message):
        self.status_label.setText(message)
        if value < 0:
            self.progress_bar.setRange(0, 0)
        else:
            self.progress_bar.setRange(0, 100)
            self.progress_bar.setValue(value)

    def _append_log(self, message):
        self.log_edit.appendPlainText(message)

    def _on_success(self, result):
        self.last_result = result
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(100)
        self.status_label.setText(result['message'])
        self.stop_button.setEnabled(result.get('status') == 'started')

    def _on_failure(self, message):
        self.progress_bar.setRange(0, 100)
        self.progress_bar.setValue(0)
        self.status_label.setText(message)
        self._append_log(message)
        if message != 'Operación cancelada.':
            QMessageBox.critical(self, 'No se pudo iniciar OSRM', message)

    def _on_thread_finished(self):
        self._set_busy(False)
        if self.last_result is not None:
            self.start_button.setEnabled(False)
            self.stop_button.setEnabled(self.last_result.get('status') == 'started')
        if self.worker is not None:
            self.worker.deleteLater()
            self.worker = None

    def cancel_operation(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.cancel_button.setEnabled(False)
            self.status_label.setText('Cancelando; espera a que termine el comando actual…')

    def stop_server(self):
        self.manager.stop()
        self.last_result = None
        self.status_label.setText('Servidor propio detenido.')
        self.start_button.setEnabled(True)
        self.stop_button.setEnabled(False)

    def shutdown(self):
        if self.worker is not None and self.worker.isRunning():
            self.worker.cancel()
            self.worker.wait(8000)
        self.manager.stop()

    def closeEvent(self, event):  # noqa: N802
        if self.worker is not None and self.worker.isRunning():
            self.cancel_operation()
            event.ignore()
            return
        event.accept()
