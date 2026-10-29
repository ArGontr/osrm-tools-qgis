# -*- coding: utf-8 -*-

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
    QgsCoordinateReferenceSystem,
    QgsFeature,
    QgsField,
    QgsFields,
    QgsGeometry,
    QgsProcessing,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterFeatureSink,
    QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsWkbTypes,
)

from ..base import OsrmAlgorithmBase, EPSG_4326
from ..osrm_client import route, OsrmError
from ..writers import write_csv, write_xlsx

HEADER = ['id_destino', 'minutos', 'km', 'estado']


class RouteOneToN(OsrmAlgorithmBase):

    def name(self):
        return 'route_one_to_n'

    def displayName(self):  # noqa: N802
        return ('Enrutar 1 origen a N destinos')

    def group(self):
        return ('Enrutamiento')

    def groupId(self):  # noqa: N802
        return 'routing'

    def shortHelpString(self):  # noqa: N802
        return (
            'Calcula la ruta por red vial (OSRM) desde el primer punto de la capa de '
            'origen hacia cada elemento de la capa de destinos (puntos o poligonos; '
            'para poligonos usa un punto garantizado dentro). Devuelve tabla CSV/XLSX '
            'con tiempo y distancia de red, y opcionalmente una capa de lineas con '
            'las geometrias de las rutas (EPSG:4326).')

    def createInstance(self):  # noqa: N802
        return RouteOneToN()

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterVectorLayer(
            'ORIGIN', ('Capa de origen (1 punto)'),
            types=[QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterVectorLayer(
            'DEST', ('Capa de destinos'),
            types=[QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            'DEST_ID', ('Campo ID en destinos'),
            parentLayerParameterName='DEST'))
        self.addParameter(QgsProcessingParameterBoolean(
            'WITH_GEOM', ('Crear capa de lineas de ruta'), defaultValue=True))
        self.addParameter(QgsProcessingParameterNumber(
            'THREADS', ('Hilos paralelos'),
            type=QgsProcessingParameterNumber.Integer, defaultValue=4,
            minValue=1, maxValue=16))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT_CSV', ('Tabla de resultados (CSV)'),
            fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT_XLSX', ('Tabla de resultados (XLSX, opcional)'),
            optional=True, fileFilter='XLSX (*.xlsx)'))
        self.addParameter(QgsProcessingParameterFeatureSink(
            'ROUTES', ('Rutas (lineas)'), optional=True))
        self.add_server_parameters()

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        origin_layer = self.parameterAsVectorLayer(parameters, 'ORIGIN', context)
        dest_layer = self.parameterAsVectorLayer(parameters, 'DEST', context)
        id_field = self.parameterAsString(parameters, 'DEST_ID', context)
        with_geom = self.parameterAsBoolean(parameters, 'WITH_GEOM', context)
        threads = self.parameterAsInt(parameters, 'THREADS', context)
        out_csv = self.parameterAsFileOutput(parameters, 'OUTPUT_CSV', context)
        out_xlsx = self.parameterAsFileOutput(parameters, 'OUTPUT_XLSX', context)
        url, profile = self.resolve_server(parameters, context)
        snap = self.resolve_snap_radius(parameters, context)

        feats = list(origin_layer.getFeatures())
        if not feats:
            raise ValueError(('La capa de origen no tiene elementos'))
        o_pt = self.representative_point(feats[0].geometry())
        o_x, o_y = o_pt.x(), o_pt.y()

        dests = []
        for f in dest_layer.getFeatures():
            p = self.representative_point(f.geometry())
            # Solo floats hacia los hilos (los objetos sip/Qt no son
            # thread-safe dentro del hilo-tarea de QGIS).
            dests.append((str(f[id_field]), p.x(), p.y()))
        if not dests:
            raise ValueError(('La capa de destinos no tiene elementos'))
        feedback.pushInfo(('Destinos: {0}').format(len(dests)))

        sink = None
        dest_id = None
        if with_geom:
            fields = QgsFields()
            fields.append(QgsField('id_destino', QVariant.String))
            fields.append(QgsField('km', QVariant.Double))
            fields.append(QgsField('minutos', QVariant.Double))
            (sink, dest_id) = self.parameterAsSink(
                parameters, 'ROUTES', context, fields,
                QgsWkbTypes.LineString, EPSG_4326)

        def worker(it):
            dest_id_s, x, y = it
            try:
                r = route(url, profile, (o_x, o_y), (x, y),
                          snap_radius=snap, with_geometry=with_geom)
                return (dest_id_s, r['duration_s'], r['distance_m'], None,
                        r.get('geometry'))
            except OsrmError as e:
                return (dest_id_s, None, None, str(e), None)
            except Exception as e:  # noqa: BLE001
                return (dest_id_s, None, None, 'error interno: {0}'.format(e), None)

        feedback.setProgressText(('Consultando OSRM (route)...'))
        results = self.run_threaded(dests, worker, threads, feedback)

        rows = []
        for item, res in zip(dests, results):
            if res is None:
                rows.append((item[0], '', '', 'error interno'))
                continue
            (dest_id_s, dur, dist, err, geom_geojson) = res
            if err is not None:
                rows.append((dest_id_s, '', '', err))
                continue
            rows.append((dest_id_s, round(dur / 60.0, 2), round(dist / 1000.0, 3), 'OK'))
            if sink is not None and geom_geojson:
                f = QgsFeature(fields)
                f.setAttribute('id_destino', dest_id_s)
                f.setAttribute('km', round(dist / 1000.0, 3))
                f.setAttribute('minutos', round(dur / 60.0, 2))
                f.setGeometry(self.line_from_geojson(geom_geojson))
                sink.addFeature(f)

        write_csv(out_csv, HEADER, rows)
        if out_xlsx:
            write_xlsx(out_xlsx, 'rutas', HEADER, out_csv)

        n_err = sum(1 for r in rows if r[3] != 'OK')
        feedback.pushInfo(('OK: {0} | con error: {1}').format(len(rows) - n_err, n_err))
        return {'OUTPUT_CSV': out_csv, 'OUTPUT_XLSX': out_xlsx, 'ROUTES': dest_id}
