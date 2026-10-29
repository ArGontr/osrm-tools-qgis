# -*- coding: utf-8 -*-
"""Cliente HTTP minimo para servidores OSRM (solo stdlib, seguro en hilos)."""
import json
import time
import urllib.error
import urllib.parse
import urllib.request

USER_AGENT = 'osrm-tools-qgis/0.1'
DEFAULT_TIMEOUT = 180


class OsrmError(Exception):
    pass


def _request_json(url, timeout=DEFAULT_TIMEOUT, retries=3, backoff=1.5):
    headers = {'User-Agent': USER_AGENT}
    last = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode('utf-8'))
        except urllib.error.HTTPError as e:
            # 4xx (salvo 429) es determinista: NoSegment, InvalidUrl, etc.
            # Reintentar solo desperdicia tiempo.
            if e.code < 500 and e.code != 429:
                raise OsrmError('HTTP {0} en {1}: {2}'.format(
                    e.code, url.split('?')[0], e.read().decode('utf-8', 'ignore')[:200]))
            last = e
        except Exception as exc:  # noqa: BLE001
            last = exc
        if attempt < retries - 1:
            time.sleep(backoff ** attempt)
    raise OsrmError('Fallo tras {0} intentos en {1}: {2}'.format(retries, url.split('?')[0], last))


def table(base_url, profile, sources, destinations, snap_radius=None, timeout=DEFAULT_TIMEOUT):
    """Matriz fuentes x destinos via servicio 'table' (GET).

    Notas de robustez (verificadas contra osrm/osrm-backend:latest):
    - osrm-routed rechaza POST ("URL string malformed"); GET soporta URLs de
      decenas de KB sin problema (probado: 2,000 coords / 1000x1000 OK).
    - NO enviar 'radiuses': con valores concretos o -1 devuelve 400
      NoSegment en coords que sin radiuses hacen snap perfecto (bug de
      table+radiuses en MLD). El snap de table es ilimitado por defecto;
      el filtrado por distancia lo hace el pre-filtro euclidiano.

    sources/destinations: [(lon, lat), ...]
    Devuelve [[(duracion_s, distancia_m), ...] por fuente]; None en celdas sin ruta.
    """
    n_src = len(sources)
    coords = list(sources) + list(destinations)
    path = ';'.join('{0},{1}'.format(lon, lat) for lon, lat in coords)
    q = {
        'sources': ';'.join(str(i) for i in range(n_src)),
        'destinations': ';'.join(str(i) for i in range(n_src, len(coords))),
        'annotations': 'duration,distance',
        'generate_hints': 'false',
    }
    url = '{0}/table/v1/{1}/{2}?{3}'.format(
        base_url.rstrip('/'), profile, path, urllib.parse.urlencode(q))
    j = _request_json(url, timeout=timeout)
    durations = j.get('durations') or []
    distances = j.get('distances') or []
    return [
        [
            (durations[i][k], distances[i][k]) if durations[i][k] is not None else (None, None)
            for k in range(len(destinations))
        ]
        for i in range(len(sources))
    ]


def route(base_url, profile, origin, destination, snap_radius=None,
          with_geometry=False, timeout=DEFAULT_TIMEOUT):
    """Una ruta origen->destino via servicio 'route' (GET).

    Devuelve dict: duration_s, distance_m, geometry (geojson dict o None).
    """
    q = {'generate_hints': 'false', 'overview': 'full' if with_geometry else 'false'}
    if with_geometry:
        q['geometries'] = 'geojson'
    if snap_radius:
        q['radiuses'] = '{0};{0}'.format(snap_radius)
    coords = '{0},{1};{2},{3}'.format(origin[0], origin[1], destination[0], destination[1])
    url = '{0}/route/v1/{1}/{2}?{3}'.format(base_url.rstrip('/'), profile, coords, urllib.parse.urlencode(q))
    j = _request_json(url, timeout=timeout)
    routes = j.get('routes') or []
    if not routes:
        raise OsrmError('OSRM no devolvio ruta')
    r = routes[0]
    return {
        'duration_s': r.get('duration'),
        'distance_m': r.get('distance'),
        'geometry': r.get('geometry') if with_geometry else None,
    }
