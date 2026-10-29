# -*- coding: utf-8 -*-

from qgis.PyQt.QtCore import QVariant
from qgis.core import (
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

HEADER = ['id_demanda', 'id_centro', 'minutos', 'km', 'estado']


class RoutesAssigned(OsrmAlgorithmBase):

    def name(self):
        return 'routes_assigned'

    def displayName(self):  # noqa: N802
        return ('Rutas para asignaciones existentes')

    def group(self):
        return ('Enrutamiento')

    def groupId(self):  # noqa: N802
        return 'routing'

    def shortHelpString(self):  # noqa: N802
        return (
            'Recibe una capa de demanda que YA tiene asignado un centro de servicio '
            '(campo con el ID del centro) y calcula, por red vial (OSRM route), la '
            'ruta real de cada elemento hacia su centro. Devuelve tabla CSV/XLSX '
            'con tiempo y distancia de red, y opcionalmente una capa de lineas con '
            'las geometrias (EPSG:4326). Util para enriquecer asignaciones hechas '
            'con otro criterio o con el algoritmo de asignacion masiva.')

    def createInstance(self):  # noqa: N802
        return RoutesAssigned()

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterVectorLayer(
            'ASSIGNED', ('Capa de demanda con asignacion (puntos o poligonos)'),
            types=[QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            'DEMAND_ID', ('Campo ID en demanda'),
            parentLayerParameterName='ASSIGNED', optional=True))
        self.addParameter(QgsProcessingParameterField(
            'CENTER_ID_FIELD', ('Campo con el ID del centro asignado'),
            parentLayerParameterName='ASSIGNED'))
        self.addParameter(QgsProcessingParameterVectorLayer(
            'CENTERS', ('Capa de centros de servicio (puntos)'),
            types=[QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            'CENTER_ID', ('Campo ID en centros'),
            parentLayerParameterName='CENTERS'))
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
        assigned_layer = self.parameterAsVectorLayer(parameters, 'ASSIGNED', context)
        centers_layer = self.parameterAsVectorLayer(parameters, 'CENTERS', context)
        dem_id_f = self.parameterAsString(parameters, 'DEMAND_ID', context)
        center_ref_f = self.parameterAsString(parameters, 'CENTER_ID_FIELD', context)
        cid = self.parameterAsString(parameters, 'CENTER_ID', context)
        with_geom = self.parameterAsBoolean(parameters, 'WITH_GEOM', context)
        threads = self.parameterAsInt(parameters, 'THREADS', context)
        out_csv = self.parameterAsFileOutput(parameters, 'OUTPUT_CSV', context)
        out_xlsx = self.parameterAsFileOutput(parameters, 'OUTPUT_XLSX', context)
        url, profile = self.resolve_server(parameters, context)
        snap = self.resolve_snap_radius(parameters, context)

        centers = {}
        for f in centers_layer.getFeatures():
            p = self.representative_point(f.geometry())
            # Solo floats hacia los hilos (los objetos sip/Qt no son
            # thread-safe dentro del hilo-tarea de QGIS).
            centers[str(f[cid])] = (p.x(), p.y())
        feedback.pushInfo(('Centros: {0}').format(len(centers)))

        pairs = []
        for f in assigned_layer.getFeatures():
            dem_id = str(f[dem_id_f]) if dem_id_f else str(f.id())
            ref = f[center_ref_f]
            if ref is None or str(ref) not in centers:
                pairs.append((dem_id, str(ref) if ref is not None else '',
                              None, None, None, None))
                continue
            c_x, c_y = centers[str(ref)]
            p = self.representative_point(f.geometry())
            pairs.append((dem_id, str(ref), p.x(), p.y(), c_x, c_y))
        feedback.pushInfo(('Pares a enrutar: {0}').format(len(pairs)))

        sink = None
        dest_id = None
        if with_geom:
            fields = QgsFields()
            fields.append(QgsField('id_demanda', QVariant.String))
            fields.append(QgsField('id_centro', QVariant.String))
            fields.append(QgsField('km', QVariant.Double))
            fields.append(QgsField('minutos', QVariant.Double))
            (sink, dest_id) = self.parameterAsSink(
                parameters, 'ROUTES', context, fields,
                QgsWkbTypes.LineString, EPSG_4326)

        def worker(it):
            dem_id, center_id, fx, fy, tx, ty = it
            if fx is None:
                return (dem_id, center_id, None, None, 'centro no encontrado', None)
            try:
                r = route(url, profile, (fx, fy), (tx, ty),
                          snap_radius=snap, with_geometry=with_geom)
                return (dem_id, center_id, r['duration_s'], r['distance_m'], 'OK',
                        r.get('geometry'))
            except OsrmError as e:
                return (dem_id, center_id, None, None, str(e), None)
            except Exception as e:  # noqa: BLE001
                return (dem_id, center_id, None, None,
                        'error interno: {0}'.format(e), None)

        feedback.setProgressText(('Consultando OSRM (route)...'))
        results = self.run_threaded(pairs, worker, threads, feedback)

        rows = []
        for item, res in zip(pairs, results):
            if res is None:
                rows.append((item[0], item[1], '', '', 'error interno'))
                continue
            (dem_id, center_id, dur, dist, estado, geom_geojson) = res
            if estado != 'OK':
                rows.append((dem_id, center_id, '', '', estado))
                continue
            rows.append((dem_id, center_id, round(dur / 60.0, 2),
                         round(dist / 1000.0, 3), estado))
            if sink is not None and geom_geojson:
                feat = QgsFeature(fields)
                feat.setAttribute('id_demanda', dem_id)
                feat.setAttribute('id_centro', center_id)
                feat.setAttribute('km', round(dist / 1000.0, 3))
                feat.setAttribute('minutos', round(dur / 60.0, 2))
                feat.setGeometry(self.line_from_geojson(geom_geojson))
                sink.addFeature(feat)

        write_csv(out_csv, HEADER, rows)
        if out_xlsx:
            write_xlsx(out_xlsx, 'rutas', HEADER, out_csv)

        n_err = sum(1 for r in rows if r[4] != 'OK')
        feedback.pushInfo(('OK: {0} | con error: {1}').format(len(rows) - n_err, n_err))
        return {'OUTPUT_CSV': out_csv, 'OUTPUT_XLSX': out_xlsx, 'ROUTES': dest_id}
