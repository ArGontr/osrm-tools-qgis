# -*- coding: utf-8 -*-
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'plugin'))

from osrm_tools.server.manager import (  # noqa: E402
    LocalServerManager, OperationCancelled, ServerError, _safe_extract_tar,
    is_osrm_running,
)


class OsrmHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        body = json.dumps({'code': 'NoSegment', 'message': 'outside test graph'}).encode()
        self.send_response(400)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


class ServerManagerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.manager = LocalServerManager(self.base / 'profile')

    def tearDown(self):
        self.manager.stop()
        self.temp.cleanup()

    def test_health_accepts_no_segment_as_live_osrm(self):
        server = HTTPServer(('127.0.0.1', 0), OsrmHandler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            self.assertTrue(is_osrm_running(
                'http://127.0.0.1:{0}'.format(server.server_port), timeout=2))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_health_rejects_closed_port(self):
        self.assertFalse(is_osrm_running('http://127.0.0.1:9', timeout=0.1))

    def test_graph_manifest_and_fingerprint_are_required(self):
        graph_dir, graph_base = self.manager.graph_paths(self.base, 'a' * 64)
        graph_dir.mkdir(parents=True)
        Path(str(graph_base) + '.osrm').write_bytes(b'graph')
        Path(str(graph_base) + '.osrm.mldgr').write_bytes(b'mld')
        self.assertFalse(self.manager.graph_is_valid(graph_dir, graph_base, 'a' * 64))
        (graph_dir / 'graph.json').write_text(json.dumps({
            'complete': True, 'pipeline': 'extract-partition-customize',
            'sha256': 'a' * 64}), encoding='utf-8')
        self.assertTrue(self.manager.graph_is_valid(graph_dir, graph_base, 'a' * 64))
        self.assertFalse(self.manager.graph_is_valid(graph_dir, graph_base, 'b' * 64))

    def test_safe_tar_extraction_rejects_traversal(self):
        archive = self.base / 'bad.tar.gz'
        with tarfile.open(archive, 'w:gz') as tar:
            info = tarfile.TarInfo('../escape.txt')
            payload = b'nope'
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        with self.assertRaises(ServerError):
            _safe_extract_tar(archive, self.base / 'out')
        self.assertFalse((self.base / 'escape.txt').exists())

    def test_platform_asset_names(self):
        cases = [
            ('Windows', 'AMD64', '-win32-x64-Release.tar.gz'),
            ('Darwin', 'arm64', '-darwin-arm64-Release.tar.gz'),
            ('Linux', 'x86_64', '-linux-x64-Release.tar.gz'),
        ]
        for system, machine, suffix in cases:
            with self.subTest(system=system, machine=machine), \
                    mock.patch('platform.system', return_value=system), \
                    mock.patch('platform.machine', return_value=machine):
                self.assertTrue(self.manager._platform_asset()[1].endswith(suffix))

    def test_silent_command_can_be_cancelled(self):
        cancel = threading.Event()
        timer = threading.Timer(0.4, cancel.set)
        timer.start()
        started = time.monotonic()
        with self.assertRaises(OperationCancelled):
            self.manager._run(
                [sys.executable, '-c', 'import time; time.sleep(30)'],
                None, lambda *args: None, cancel)
        timer.cancel()
        self.assertLess(time.monotonic() - started, 5)

    def test_geofabrik_url_is_restricted(self):
        with self.assertRaises(ServerError):
            self.manager.resolve_pbf({
                'data_dir': str(self.base),
                'pbf_url': 'https://example.com/map.osm.pbf'}, lambda *args: None, None)

    def test_native_pipeline_runs_in_order_and_reuses_graph(self):
        pbf = self.base / 'monaco.osm.pbf'
        pbf.write_bytes(b'fake-pbf-for-command-test')
        tools = {name: self.base / name for name in
                 ('osrm-extract', 'osrm-partition', 'osrm-customize', 'osrm-routed')}
        commands = []

        def fake_run(command, *args, **kwargs):
            commands.append(Path(command[0]).name)
            if Path(command[0]).name == 'osrm-extract':
                output = Path(command[command.index('--output') + 1])
                Path(str(output) + '.osrm').write_bytes(b'graph')
            if Path(command[0]).name == 'osrm-customize':
                graph = Path(command[-1])
                Path(str(graph) + '.mldgr').write_bytes(b'mld')

        config = {'backend': 'native', 'pbf_path': str(pbf),
                  'pbf_url': '', 'data_dir': str(self.base / 'data')}
        with mock.patch.object(self.manager, '_find_native_tools', return_value=tools), \
                mock.patch.object(self.manager, '_find_profile', return_value=self.base / 'car.lua'), \
                mock.patch.object(self.manager, '_run', side_effect=fake_run):
            self.manager.prepare_graph(config, lambda *args: None, None)
            self.manager.prepare_graph(config, lambda *args: None, None)
        self.assertEqual(commands, ['osrm-extract', 'osrm-partition', 'osrm-customize'])

    def test_native_install_extracts_verified_archive(self):
        suffix = self.manager._platform_asset()[1]
        name = 'osrm-v1.2.3' + suffix
        archive = self.manager.base_dir / 'downloads' / name
        archive.parent.mkdir(parents=True)
        with tarfile.open(archive, 'w:gz') as tar:
            executable_suffix = '.exe' if os.name == 'nt' else ''
            for tool in ('osrm-extract', 'osrm-partition', 'osrm-customize', 'osrm-routed'):
                payload = b'fake executable'
                info = tarfile.TarInfo('package/' + tool + executable_suffix)
                info.size = len(payload)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(payload))
            payload = b'-- fake profile'
            info = tarfile.TarInfo('package/profiles/car.lua')
            info.size = len(payload)
            tar.addfile(info, io.BytesIO(payload))
        digest = hashlib.sha256(archive.read_bytes()).hexdigest()
        release = {'tag_name': 'v1.2.3', 'assets': [{
            'name': name, 'digest': 'sha256:' + digest,
            'browser_download_url': 'https://example.invalid/' + name}]}
        response = io.BytesIO(json.dumps(release).encode())
        with mock.patch('urllib.request.urlopen', return_value=response), \
                mock.patch.object(self.manager, '_run'):
            tools = self.manager._install_native(lambda *args: None, None)
        self.assertEqual(set(tools), {'osrm-extract', 'osrm-partition',
                                      'osrm-customize', 'osrm-routed'})
        self.assertTrue(all(path.is_file() for path in tools.values()))

    def test_docker_pipeline_uses_official_image_and_three_steps(self):
        pbf = self.base / 'map.osm.pbf'
        pbf.write_bytes(b'pbf')
        graph_dir = self.base / 'graph'
        graph_dir.mkdir()
        commands = []
        with mock.patch.object(self.manager, 'docker_executable', return_value='docker'), \
                mock.patch.object(self.manager, 'docker_running', return_value=True), \
                mock.patch('subprocess.run', return_value=mock.Mock(returncode=0)), \
                mock.patch.object(self.manager, '_run',
                                  side_effect=lambda command, *args: commands.append(command)):
            self.manager._build_graph_docker(
                pbf, graph_dir, lambda *args: None, None,
                'ghcr.io/project-osrm/osrm-backend:latest')
        steps = []
        for command in commands:
            steps.extend(tool for tool in ('osrm-extract', 'osrm-partition', 'osrm-customize')
                         if tool in command)
        self.assertEqual(steps, ['osrm-extract', 'osrm-partition', 'osrm-customize'])


if __name__ == '__main__':
    unittest.main()
