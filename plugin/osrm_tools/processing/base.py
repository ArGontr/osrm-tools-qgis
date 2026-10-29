# -*- coding: utf-8 -*-
"""Clase base con parametros y utilidades comunes de los algoritmos OSRM."""
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsDistanceArea,
    QgsGeometry,
    QgsPointXY,
    QgsProcessingAlgorithm,
    QgsProcessingParameterEnum,
    QgsProcessingParameterNumber,
    QgsProcessingParameterString,
    QgsWkbTypes,
)

# (etiqueta, url por defecto, perfil OSRM)
PROFILES = [
    ('Auto (localhost:5000)', 'http://localhost:5000', 'driving'),
    ('A pie (localhost:5001)', 'http://localhost:5001', 'foot'),
]
PERFIL_PERSONALIZADO = 'Personalizada'

EPSG_4326 = QgsCoordinateReferenceSystem.fromEpsgId(4326)


def haversine_km(x1, y1, x2, y2):
    """Distancia esferica (km) entre dos puntos lon/lat. Pura stdlib, segura
    de usar dentro de hilos Python (nada de objetos sip/Qt)."""
    R = 6371.0088
    p1, p2 = math.radians(y1), math.radians(y2)
    dp = p2 - p1
    dl = math.radians(x2 - x1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * R * math.asin(math.sqrt(a))


class OsrmAlgorithmBase(QgsProcessingAlgorithm):

    def add_server_parameters(self, with_snap=True):
        self.addParameter(QgsProcessingParameterEnum(
            'PERFIL', ('Servidor OSRM'),
            options=[p[0] for p in PROFILES] + [PERFIL_PERSONALIZADO],
            defaultValue=0))
        self.addParameter(QgsProcessingParameterString(
            'URL', ('URL personalizada (solo si Servidor = Personalizada)'),
            defaultValue='http://localhost:5000', optional=True))
        if with_snap:
            self.addParameter(QgsProcessingParameterNumber(
                'SNAP_RADIUS', ('Radio de snap a la vialidad en "route" (m; 0 = ilimitado)'),
                type=QgsProcessingParameterNumber.Integer, defaultValue=350, minValue=0))

    def resolve_server(self, parameters, context):
        idx = self.parameterAsEnum(parameters, 'PERFIL', context)
        if idx == len(PROFILES):
            url = self.parameterAsString(parameters, 'URL', context).strip() \
                or 'http://localhost:5000'
            return url.rstrip('/'), 'driving'
        return PROFILES[idx][1], PROFILES[idx][2]

    def resolve_snap_radius(self, parameters, context):
        r = self.parameterAsInt(parameters, 'SNAP_RADIUS', context)
        return r if r > 0 else None

    @staticmethod
    def representative_point(geom):
        """QgsPointXY representativo: vertice si es punto, point-on-surface si no."""
        if geom.type() == QgsWkbTypes.PointGeometry:
            if geom.isMultipart():
                return geom.asMultiPoint()[0]
            return geom.asPoint()
        return geom.pointOnSurface().asPoint()

    @staticmethod
    def line_from_geojson(geom):
        """LineString OSRM (geojson dict) -> QgsGeometry (sin depender de fromJson)."""
        coords = geom.get('coordinates') or []
        return QgsGeometry.fromPolylineXY(
            [QgsPointXY(float(x), float(y)) for x, y in coords])

    @staticmethod
    def distance_area(context):
        da = QgsDistanceArea()
        da.setSourceCrs(EPSG_4326, context.transformContext())
        da.setEllipsoid('WGS84')
        return da

    def run_threaded(self, items, worker, threads, feedback):
        """Ejecuta worker(item) en hilos Python; progreso y cancelacion entre items.

        REGLA DURA: los workers corren dentro del hilo-tarea de QGIS, asi que
        NINGUN objeto sip/Qt (QgsGeometry, QgsPointXY, QgsDistanceArea,
        QgsFeature, capas, feedback...) puede tocarse desde ellos: solo tipos
        Python puros (floats, str, tuplas) + urllib. Un worker nunca debe
        lanzar: convierte sus fallos en valores de error. Si algo aun asi
        escapa, ese item queda en None (el llamador lo marca como error
        generico); una excepcion cruzando hacia los hilos de QGIS tumba la
        app con access violation en vez de mostrar un error amable.
        """
        results = [None] * len(items)
        total = max(1, len(items))
        done = 0
        with ThreadPoolExecutor(max_workers=max(1, min(threads, 16))) as ex:
            futures = {ex.submit(worker, it): i for i, it in enumerate(items)}
            for fut in as_completed(futures):
                if feedback.isCanceled():
                    for f in futures:
                        f.cancel()
                    break
                i = futures[fut]
                try:
                    results[i] = fut.result()
                except Exception as e:  # noqa: BLE001
                    results[i] = None
                    feedback.reportError(('Fallo interno en worker: {0}').format(e))
                done += 1
                feedback.setProgress(done / total * 100.0)
        return results
