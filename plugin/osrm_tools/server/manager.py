# -*- coding: utf-8 -*-
"""Núcleo sin Qt para instalar OSRM, preparar grafos y controlar el servidor."""
import hashlib
import json
import os
import platform
import queue
import shutil
import socket
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

USER_AGENT = 'osrm-tools-qgis/0.2'
DOCKER_IMAGE = 'ghcr.io/project-osrm/osrm-backend:latest'
BINARIES_API = 'https://api.github.com/repos/e-kotov/osrm-binaries/releases/latest'
GEOFABRIK_HOST = 'download.geofabrik.de'
TOOLS = ('osrm-extract', 'osrm-partition', 'osrm-customize', 'osrm-routed')


class ServerError(Exception):
    """Error esperado, apto para mostrar al usuario."""


class OperationCancelled(ServerError):
    """Operación cancelada por el usuario."""


def _check_cancelled(cancel_event):
    if cancel_event is not None and cancel_event.is_set():
        raise OperationCancelled('Operación cancelada.')


def _sha256(path, cancel_event=None):
    digest = hashlib.sha256()
    with open(str(path), 'rb') as stream:
        while True:
            _check_cancelled(cancel_event)
            block = stream.read(4 * 1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _safe_extract_tar(archive, destination):
    """Extrae sin permitir rutas absolutas, traversals ni enlaces externos."""
    destination = Path(destination).resolve()
    with tarfile.open(str(archive), 'r:gz') as tar:
        members = tar.getmembers()
        for member in members:
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise ServerError('El archivo descargado contiene una ruta insegura.')
            if member.islnk() or member.issym():
                link_target = (target.parent / member.linkname).resolve()
                if link_target != destination and destination not in link_target.parents:
                    raise ServerError('El archivo descargado contiene un enlace inseguro.')
            if member.isdev():
                raise ServerError('El archivo descargado contiene un dispositivo no permitido.')
        # Las rutas ya se validaron; fully_trusted conserva permisos de binarios.
        if sys.version_info >= (3, 12):
            tar.extractall(str(destination), members=members, filter='fully_trusted')
        else:
            tar.extractall(str(destination), members=members)  # nosec: rutas validadas


def is_osrm_running(url='http://127.0.0.1:5000', timeout=1.5):
    """OSRM no tiene /health; una respuesta OSRM válida también sirve como prueba."""
    probe = url.rstrip('/') + '/route/v1/driving/0,0;0,0?overview=false'
    request = urllib.request.Request(probe, headers={'User-Agent': USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            payload = json.loads(response.read().decode('utf-8'))
    except urllib.error.HTTPError as exc:
        try:
            payload = json.loads(exc.read().decode('utf-8'))
        except Exception:  # noqa: BLE001
            return False
    except Exception:  # noqa: BLE001
        return False
    return payload.get('code') in ('Ok', 'NoRoute', 'NoSegment')


def port_is_free(port, host='127.0.0.1'):
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, int(port))) != 0


class LocalServerManager:
    """Administra únicamente procesos/contenedores creados por esta instancia."""

    def __init__(self, base_dir, logger=None):
        self.base_dir = Path(base_dir).expanduser().resolve()
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir = self.base_dir / 'logs'
        self.logs_dir.mkdir(exist_ok=True)
        self.logger = logger or (lambda message: None)
        self._process = None
        self._container = None
        self._lock = threading.RLock()

    @staticmethod
    def docker_executable():
        return shutil.which('docker')

    def docker_running(self):
        executable = self.docker_executable()
        if not executable:
            return False
        try:
            result = subprocess.run(
                [executable, 'version', '--format', '{{.Server.Version}}'],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=8,
                check=False, creationflags=self._creationflags())
            return result.returncode == 0
        except (OSError, subprocess.TimeoutExpired):
            return False

    @staticmethod
    def _creationflags():
        return subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0

    def _log(self, message):
        self.logger(str(message).rstrip())

    def _download(self, url, destination, progress, cancel_event):
        destination = Path(destination)
        destination.parent.mkdir(parents=True, exist_ok=True)
        partial = destination.with_name(destination.name + '.part')
        request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
        try:
            with urllib.request.urlopen(request, timeout=90) as response, \
                    open(str(partial), 'wb') as stream:
                total = int(response.headers.get('Content-Length') or 0)
                done = 0
                while True:
                    _check_cancelled(cancel_event)
                    block = response.read(1024 * 1024)
                    if not block:
                        break
                    stream.write(block)
                    done += len(block)
                    percent = int(done * 100 / total) if total else -1
                    progress(percent, 'Descargando {0}: {1:.1f} MiB'.format(
                        destination.name, done / 1048576.0))
            os.replace(str(partial), str(destination))
        except Exception:
            try:
                partial.unlink()
            except OSError:
                pass
            raise

    @staticmethod
    def _platform_asset():
        system = platform.system().lower()
        machine = platform.machine().lower()
        if system.startswith('win'):
            system = 'win32'
        elif system == 'darwin':
            system = 'darwin'
        elif system == 'linux':
            system = 'linux'
        else:
            raise ServerError('Sistema operativo no soportado: {0}'.format(system))
        if machine in ('x86_64', 'amd64', 'x64'):
            architecture = 'x64'
        elif machine in ('aarch64', 'arm64') and system != 'win32':
            architecture = 'arm64'
        else:
            raise ServerError('Arquitectura no soportada: {0}'.format(machine))
        return 'osrm-', '-{0}-{1}-Release.tar.gz'.format(system, architecture)

    def _find_native_tools(self, binary_dir=None):
        executable_suffix = '.exe' if os.name == 'nt' else ''
        candidates = []
        if binary_dir:
            candidates.append(Path(binary_dir).expanduser())
        managed = self.base_dir / 'bin'
        if managed.exists():
            candidates.extend(sorted(
                (p for p in managed.iterdir() if p.is_dir()), reverse=True))
        for candidate in candidates:
            direct = candidate
            search = [direct]
            if direct.exists():
                search.extend(p for p in direct.iterdir() if p.is_dir())
            for folder in search:
                tools = {name: folder / (name + executable_suffix) for name in TOOLS}
                if all(path.is_file() for path in tools.values()):
                    return tools
        path_tools = {}
        for name in TOOLS:
            found = shutil.which(name)
            if not found:
                break
            path_tools[name] = Path(found)
        return path_tools if len(path_tools) == len(TOOLS) else None

    def _install_native(self, progress, cancel_event):
        progress(-1, 'Consultando la versión más reciente de OSRM…')
        request = urllib.request.Request(
            BINARIES_API, headers={'User-Agent': USER_AGENT,
                                   'Accept': 'application/vnd.github+json'})
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                release = json.loads(response.read().decode('utf-8'))
        except urllib.error.HTTPError as exc:
            raise ServerError('GitHub no permitió consultar OSRM ({0}). '
                              'Reintenta o usa Docker.'.format(exc.code))
        prefix, suffix = self._platform_asset()
        assets = release.get('assets') or []
        asset = next((a for a in assets if a.get('name', '').startswith(prefix)
                      and a.get('name', '').endswith(suffix)), None)
        if not asset:
            raise ServerError('No hay binarios OSRM para este sistema en la versión {0}.'
                              .format(release.get('tag_name', 'actual')))
        tag = ''.join(c if c.isalnum() or c in '.-_' else '_'
                      for c in release.get('tag_name', 'latest'))
        install_dir = self.base_dir / 'bin' / tag
        existing = self._find_native_tools(install_dir)
        if existing:
            return existing

        downloads = self.base_dir / 'downloads'
        archive = downloads / asset['name']
        if not archive.is_file():
            self._download(asset['browser_download_url'], archive, progress, cancel_event)
        expected = (asset.get('digest') or '').removeprefix('sha256:')
        if not expected:
            expected = self._checksum_from_release(assets, asset['name'], cancel_event)
        progress(-1, 'Verificando la descarga…')
        actual = _sha256(archive, cancel_event)
        if actual.lower() != expected.lower():
            try:
                archive.unlink()
            except OSError:
                pass
            raise ServerError('La suma SHA-256 de OSRM no coincide. Descarga eliminada.')

        install_dir.parent.mkdir(parents=True, exist_ok=True)
        temporary = Path(tempfile.mkdtemp(prefix='.extract-', dir=str(install_dir.parent)))
        staging = install_dir.with_name('.' + install_dir.name + '.staging')
        try:
            _safe_extract_tar(archive, temporary)
            roots = [temporary] + [p for p in temporary.rglob('*') if p.is_dir()]
            suffix_exe = '.exe' if os.name == 'nt' else ''
            root = next((p for p in roots if all(
                (p / (name + suffix_exe)).is_file() for name in TOOLS)), None)
            if root is None:
                raise ServerError('El paquete no contiene todas las herramientas OSRM.')
            if staging.exists():
                shutil.rmtree(str(staging))
            shutil.copytree(str(root), str(staging))
            if os.name != 'nt':
                for name in TOOLS:
                    (staging / name).chmod(0o755)
            if install_dir.exists():
                shutil.rmtree(str(install_dir))
            os.replace(str(staging), str(install_dir))
        finally:
            shutil.rmtree(str(temporary), ignore_errors=True)
            if staging.exists():
                shutil.rmtree(str(staging), ignore_errors=True)
        tools = self._find_native_tools(install_dir)
        if not tools:
            raise ServerError('No se pudo completar la instalación local de OSRM.')
        self._run([str(tools['osrm-routed']), '--version'], None,
                  progress, cancel_event, timeout=30)
        return tools

    def _checksum_from_release(self, assets, asset_name, cancel_event):
        checksum_asset = next((a for a in assets if a.get('name') == 'checksums.txt'), None)
        if not checksum_asset:
            raise ServerError('La versión de OSRM no publica una suma SHA-256.')
        destination = self.base_dir / 'downloads' / 'checksums-{0}.txt'.format(asset_name)
        self._download(checksum_asset['browser_download_url'], destination,
                       lambda *args: None, cancel_event)
        for line in destination.read_text(encoding='utf-8').splitlines():
            parts = line.replace('*', ' ').split()
            if len(parts) >= 2 and parts[-1] == asset_name:
                return parts[0]
        raise ServerError('No se encontró la suma SHA-256 del paquete OSRM.')

    @staticmethod
    def graph_paths(data_dir, fingerprint):
        graph_dir = Path(data_dir) / 'graphs' / fingerprint[:16]
        return graph_dir, graph_dir / 'driving'

    @staticmethod
    def graph_is_valid(graph_dir, graph_base, fingerprint=None):
        manifest_path = Path(graph_dir) / 'graph.json'
        required = [Path(str(graph_base) + '.osrm'), Path(str(graph_base) + '.osrm.mldgr')]
        if not all(path.is_file() and path.stat().st_size > 0 for path in required):
            return False
        try:
            manifest = json.loads(manifest_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            return False
        return (manifest.get('complete') is True
                and manifest.get('pipeline') == 'extract-partition-customize'
                and (fingerprint is None or manifest.get('sha256') == fingerprint))

    def resolve_pbf(self, config, progress, cancel_event):
        data_dir = Path(config['data_dir']).expanduser().resolve()
        data_dir.mkdir(parents=True, exist_ok=True)
        url = (config.get('pbf_url') or '').strip()
        if url:
            parsed = urllib.parse.urlparse(url)
            if (parsed.scheme != 'https' or parsed.hostname != GEOFABRIK_HOST
                    or not parsed.path.endswith('.osm.pbf')):
                raise ServerError('Usa una URL HTTPS de download.geofabrik.de que termine en .osm.pbf.')
            destination = data_dir / 'sources' / Path(parsed.path).name
            if not destination.is_file():
                self._download(url, destination, progress, cancel_event)
            else:
                progress(-1, 'Reutilizando el PBF ya descargado: {0}'.format(destination.name))
            return destination
        pbf = Path(config.get('pbf_path') or '').expanduser()
        if not pbf.is_file() or not pbf.name.endswith('.osm.pbf'):
            raise ServerError('Selecciona un archivo .osm.pbf válido.')
        return pbf.resolve()

    def prepare_graph(self, config, progress, cancel_event):
        pbf = self.resolve_pbf(config, progress, cancel_event)
        progress(-1, 'Calculando la huella del archivo OSM…')
        fingerprint = _sha256(pbf, cancel_event)
        graph_dir, graph_base = self.graph_paths(config['data_dir'], fingerprint)
        if self.graph_is_valid(graph_dir, graph_base, fingerprint):
            progress(100, 'Grafo funcional encontrado; no hace falta reprocesarlo.')
            return graph_dir, graph_base, fingerprint

        # Un grafo incompleto no se mezcla con archivos de un intento anterior.
        if graph_dir.exists():
            shutil.rmtree(str(graph_dir))
        graph_dir.mkdir(parents=True, exist_ok=True)
        manifest_path = graph_dir / 'graph.json'
        backend = config.get('backend', 'docker')
        if backend == 'docker':
            self._build_graph_docker(pbf, graph_dir, progress, cancel_event,
                                     config.get('docker_image') or DOCKER_IMAGE)
        elif backend == 'native':
            tools = self._find_native_tools(config.get('binary_dir'))
            if not tools:
                progress(-1, 'Instalando los binarios OSRM para este equipo…')
                tools = self._install_native(progress, cancel_event)
            self._build_graph_native(tools, pbf, graph_dir, graph_base,
                                     progress, cancel_event)
        else:
            raise ServerError('Motor no reconocido: {0}'.format(backend))
        if not self.graph_is_valid_files(graph_base):
            raise ServerError('OSRM terminó sin generar un grafo MLD completo. Revisa el registro.')
        manifest = {
            'complete': True,
            'pipeline': 'extract-partition-customize',
            'algorithm': 'MLD',
            'profile': 'car',
            'sha256': fingerprint,
            'source': str(pbf),
            'created': int(time.time()),
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding='utf-8')
        progress(100, 'Grafo OSRM preparado correctamente.')
        return graph_dir, graph_base, fingerprint

    @staticmethod
    def graph_is_valid_files(graph_base):
        return all(Path(str(graph_base) + suffix).is_file()
                   and Path(str(graph_base) + suffix).stat().st_size > 0
                   for suffix in ('.osrm', '.osrm.mldgr'))

    def _build_graph_native(self, tools, pbf, graph_dir, graph_base,
                            progress, cancel_event):
        profile = self._find_profile(tools['osrm-routed'])
        commands = [
            [str(tools['osrm-extract']), '--profile', str(profile),
             '--output', str(graph_base), str(pbf)],
            [str(tools['osrm-partition']), str(graph_base) + '.osrm'],
            [str(tools['osrm-customize']), str(graph_base) + '.osrm'],
        ]
        for index, command in enumerate(commands):
            progress(-1, ('Ejecutando osrm-extract (puede tardar bastante)…',
                          'Ejecutando osrm-partition…',
                          'Ejecutando osrm-customize…')[index])
            self._run(command, graph_dir, progress, cancel_event)

    @staticmethod
    def _find_profile(routed_path):
        routed_path = Path(routed_path).resolve()
        candidates = [routed_path.parent / 'profiles' / 'car.lua',
                      routed_path.parent.parent / 'profiles' / 'car.lua']
        candidates.extend(routed_path.parent.rglob('car.lua'))
        for candidate in candidates:
            if candidate.is_file():
                return candidate
        raise ServerError('No se encontró profiles/car.lua junto a los binarios OSRM.')

    @staticmethod
    def _docker_volume(path):
        return str(Path(path).resolve()).replace('\\', '/')

    def _build_graph_docker(self, pbf, graph_dir, progress, cancel_event, image):
        executable = self.docker_executable()
        if not executable:
            raise ServerError('Docker no está instalado. Instálalo o elige Binarios OSRM.')
        if not self.docker_running():
            raise ServerError('Docker está instalado, pero su servicio no responde. '
                              'Abre Docker Desktop y reintenta.')
        try:
            subprocess.run([executable, 'image', 'inspect', image],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           check=True, creationflags=self._creationflags())
        except subprocess.CalledProcessError:
            progress(-1, 'Descargando la imagen oficial de OSRM…')
            self._run([executable, 'pull', image], None, progress, cancel_event)
        input_mount = self._docker_volume(pbf.parent) + ':/input:ro'
        data_mount = self._docker_volume(graph_dir) + ':/data'
        common = [executable, 'run', '--rm', '-v', input_mount, '-v', data_mount, image]
        container_pbf = '/input/' + pbf.name
        commands = [
            common + ['osrm-extract', '--profile', '/opt/car.lua',
                      '--output', '/data/driving', container_pbf],
            common + ['osrm-partition', '/data/driving.osrm'],
            common + ['osrm-customize', '/data/driving.osrm'],
        ]
        for index, command in enumerate(commands):
            progress(-1, ('Docker: extrayendo la red vial (puede tardar bastante)…',
                          'Docker: particionando el grafo…',
                          'Docker: personalizando el grafo…')[index])
            self._run(command, graph_dir, progress, cancel_event)

    def _run(self, command, cwd, progress, cancel_event, timeout=None):
        self._log('$ ' + ' '.join(str(part) for part in command))
        try:
            process = subprocess.Popen(
                [str(part) for part in command], cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                universal_newlines=True, encoding='utf-8', errors='replace',
                creationflags=self._creationflags())
        except OSError as exc:
            raise ServerError('No se pudo ejecutar {0}: {1}'.format(command[0], exc))
        started = time.time()
        output = queue.Queue()
        finished = object()

        def read_output():
            if process.stdout is not None:
                for output_line in iter(process.stdout.readline, ''):
                    output.put(output_line)
            output.put(finished)

        reader = threading.Thread(target=read_output, daemon=True)
        reader.start()
        try:
            while True:
                _check_cancelled(cancel_event)
                if timeout and time.time() - started > timeout:
                    raise ServerError('El comando excedió el tiempo permitido.')
                try:
                    line = output.get(timeout=0.2)
                except queue.Empty:
                    continue
                if line is finished:
                    break
                self._log(line)
            code = process.wait()
        except Exception:
            try:
                process.terminate()
                process.wait(timeout=5)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except OSError:
                    pass
            raise
        finally:
            reader.join(timeout=2)
            if process.stdout is not None:
                process.stdout.close()
        if code != 0:
            raise ServerError('El comando {0} terminó con código {1}. Revisa el registro.'
                              .format(Path(str(command[0])).name, code))

    def prepare_and_start(self, config, progress=None, cancel_event=None):
        progress = progress or (lambda *args: None)
        port = int(config.get('port', 5000))
        url = 'http://127.0.0.1:{0}'.format(port)
        if is_osrm_running(url):
            return {'url': url, 'status': 'existing',
                    'message': 'Ya había un servidor OSRM activo; no se creará otro.'}
        if not port_is_free(port):
            raise ServerError('El puerto {0} está ocupado por otro programa.'.format(port))
        try:
            graph_dir, graph_base, fingerprint = self.prepare_graph(
                config, progress, cancel_event)
            _check_cancelled(cancel_event)
            if config.get('backend', 'docker') == 'docker':
                self._start_docker(graph_base, port, progress, cancel_event,
                                   config.get('docker_image') or DOCKER_IMAGE)
            else:
                tools = self._find_native_tools(config.get('binary_dir'))
                if not tools:
                    tools = self._install_native(progress, cancel_event)
                self._start_native(tools['osrm-routed'], graph_base, port,
                                   progress, cancel_event)
            self._wait_until_ready(url, progress, cancel_event)
            progress(100, 'Servidor OSRM activo en ' + url)
            return {'url': url, 'status': 'started', 'graph': str(graph_base) + '.osrm',
                    'sha256': fingerprint, 'message': 'Servidor OSRM iniciado en ' + url}
        except Exception:
            self.stop()
            raise

    def _start_native(self, routed, graph_base, port, progress, cancel_event):
        log_path = self.logs_dir / 'osrm-native-{0}.log'.format(port)
        log_stream = open(str(log_path), 'a', encoding='utf-8')
        command = [str(routed), '--algorithm=MLD', '--max-table-size=10000',
                   '--ip=127.0.0.1', '--port={0}'.format(port),
                   str(graph_base) + '.osrm']
        self._log('$ ' + ' '.join(command))
        kwargs = {'cwd': str(graph_base.parent), 'stdout': log_stream,
                  'stderr': subprocess.STDOUT, 'creationflags': self._creationflags()}
        if os.name != 'nt':
            kwargs['start_new_session'] = True
        try:
            process = subprocess.Popen(command, **kwargs)
        except OSError as exc:
            log_stream.close()
            raise ServerError('No se pudo iniciar osrm-routed: {0}'.format(exc))
        log_stream.close()  # El hijo conserva su propio descriptor.
        with self._lock:
            self._process = process
        progress(-1, 'Iniciando osrm-routed; registro: {0}'.format(log_path))

    def _container_name(self, port):
        return 'osrm-tools-qgis-{0}'.format(port)

    def _managed_container(self, port):
        executable = self.docker_executable()
        if not executable:
            return False
        name = self._container_name(port)
        try:
            result = subprocess.run(
                [executable, 'inspect', '--type', 'container', '--format',
                 '{{index .Config.Labels "org.osrm-tools-qgis.managed"}}', name],
                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                universal_newlines=True, timeout=8, check=False,
                creationflags=self._creationflags())
            return result.returncode == 0 and result.stdout.strip() == 'true'
        except (OSError, subprocess.TimeoutExpired):
            return False

    def adopt_owned_container(self, port=5000):
        """Recupera un contenedor propio que siga vivo tras recargar el plugin."""
        if self._managed_container(port):
            with self._lock:
                self._container = self._container_name(port)
            return True
        return False

    def _start_docker(self, graph_base, port, progress, cancel_event, image):
        executable = self.docker_executable()
        if not executable:
            raise ServerError('Docker no está instalado. Instálalo o elige Binarios OSRM.')
        if not self.docker_running():
            raise ServerError('Docker no responde. Abre Docker Desktop y reintenta.')
        name = self._container_name(port)
        if self._managed_container(port):
            self._run([executable, 'rm', '-f', name], None, progress, cancel_event)
        command = [
            executable, 'run', '--detach', '--rm', '--name', name,
            '--label', 'org.osrm-tools-qgis.managed=true',
            '--publish', '127.0.0.1:{0}:5000'.format(port),
            '--volume', self._docker_volume(graph_base.parent) + ':/data:ro',
            image, 'osrm-routed', '--algorithm=MLD', '--max-table-size=10000',
            '/data/' + graph_base.name + '.osrm']
        progress(-1, 'Iniciando el contenedor {0}…'.format(name))
        self._run(command, None, progress, cancel_event, timeout=60)
        with self._lock:
            self._container = name

    def _wait_until_ready(self, url, progress, cancel_event, timeout=90):
        deadline = time.time() + timeout
        while time.time() < deadline:
            _check_cancelled(cancel_event)
            with self._lock:
                process = self._process
            if process is not None and process.poll() is not None:
                raise ServerError('osrm-routed se cerró al arrancar. Revisa el registro en {0}.'
                                  .format(self.logs_dir))
            if is_osrm_running(url, timeout=1.0):
                return
            progress(-1, 'Esperando a que OSRM cargue el grafo…')
            time.sleep(1.0)
        raise ServerError('OSRM no respondió en {0} después de {1} segundos.'
                          .format(url, timeout))

    def owns_server(self):
        with self._lock:
            return self._process is not None or self._container is not None

    def stop(self):
        """Detiene sólo lo creado por el plugin; nunca un OSRM externo."""
        with self._lock:
            process = self._process
            container = self._container
            self._process = None
            self._container = None
        if process is not None and process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=10)
            except (OSError, subprocess.TimeoutExpired):
                try:
                    process.kill()
                    process.wait(timeout=5)
                except OSError:
                    pass
        if container:
            executable = self.docker_executable()
            if executable:
                try:
                    subprocess.run([executable, 'stop', '--time', '10', container],
                                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   timeout=20, check=False,
                                   creationflags=self._creationflags())
                except (OSError, subprocess.TimeoutExpired):
                    pass
