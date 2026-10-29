# -*- coding: utf-8 -*-
from qgis.core import (
    QgsProcessing,
    QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
)

from ..base import OsrmAlgorithmBase
from ..osrm_client import table, OsrmError
from ..writers import write_csv, write_xlsx

HEADER = ['id_origen', 'id_destino', 'minutos', 'km']


class MatrixMXN(OsrmAlgorithmBase):

    def name(self):
        return 'matrix_mxn'

    def displayName(self):  # noqa: N802
        return ('Matriz de tiempos/distancias MxN')

    def group(self):
        return ('Matrices y asignacion')

    def groupId(self):  # noqa: N802
        return 'assignment'

    def shortHelpString(self):  # noqa: N802
        return (
            'Calcula la matriz completa tiempos/distancias de red (OSRM table) entre '
            'dos capas: fuentes (origenes) y destinos. Las peticiones se trocean '
            'respetando el limite de localidades por llamada del servidor '
            '(--max-table-size). Salida: tabla larga CSV/XLSX con una fila por par '
            'origen-destino.')

    def createInstance(self):  # noqa: N802
        return MatrixMXN()

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterVectorLayer(
            'SOURCES', ('Capa de origenes (fuentes)'),
            types=[QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            'SOURCE_ID', ('Campo ID en origenes'),
            parentLayerParameterName='SOURCES'))
        self.addParameter(QgsProcessingParameterVectorLayer(
            'DESTS', ('Capa de destinos'),
            types=[QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            'DEST_ID', ('Campo ID en destinos'),
            parentLayerParameterName='DESTS'))
        self.addParameter(QgsProcessingParameterNumber(
            'MAX_LOCS', ('Max. localidades por llamada al servidor'),
            type=QgsProcessingParameterNumber.Integer, defaultValue=9000,
            minValue=100))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT_CSV', ('Matriz (CSV)'), fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT_XLSX', ('Matriz (XLSX, opcional)'),
            optional=True, fileFilter='XLSX (*.xlsx)'))
        self.add_server_parameters()

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        src_layer = self.parameterAsVectorLayer(parameters, 'SOURCES', context)
        dst_layer = self.parameterAsVectorLayer(parameters, 'DESTS', context)
        sid = self.parameterAsString(parameters, 'SOURCE_ID', context)
        did = self.parameterAsString(parameters, 'DEST_ID', context)
        max_locs = self.parameterAsInt(parameters, 'MAX_LOCS', context)
        out_csv = self.parameterAsFileOutput(parameters, 'OUTPUT_CSV', context)
        out_xlsx = self.parameterAsFileOutput(parameters, 'OUTPUT_XLSX', context)
        url, profile = self.resolve_server(parameters, context)
        snap = self.resolve_snap_radius(parameters, context)

        sources = [(str(f[sid]), self.representative_point(f.geometry()))
                   for f in src_layer.getFeatures()]
        dests = [(str(f[did]), self.representative_point(f.geometry()))
                 for f in dst_layer.getFeatures()]
        if not sources or not dests:
            raise ValueError(('Origenes y destinos deben tener elementos'))
        feedback.pushInfo(('Origenes: {0} | Destinos: {1}').format(len(sources), len(dests)))

        s_step = min(len(sources), max(1, max_locs // 2))
        d_step = max(1, min(max_locs - s_step, 1000000 // s_step))

        rows = []
        n_calls = 0
        for s0 in range(0, len(sources), s_step):
            for d0 in range(0, len(dests), d_step):
                if feedback.isCanceled():
                    return {}
                s_chunk = sources[s0:s0 + s_step]
                d_chunk = dests[d0:d0 + d_step]
                feedback.pushInfo((
                    'Bloque origenes {0}-{1} x destinos {2}-{3}').format(
                    s0, s0 + len(s_chunk) - 1, d0, d0 + len(d_chunk) - 1))
                matrix = table(
                    url, profile,
                    [(p.x(), p.y()) for _, p in s_chunk],
                    [(p.x(), p.y()) for _, p in d_chunk])
                n_calls += 1
                for i, (sid_v, _) in enumerate(s_chunk):
                    for k, (did_v, _) in enumerate(d_chunk):
                        dur, dist = matrix[i][k]
                        if dur is None:
                            continue
                        rows.append((sid_v, did_v, round(dur / 60.0, 2),
                                     round(dist / 1000.0, 3)))
        feedback.setProgress(100)

        write_csv(out_csv, HEADER, rows)
        if out_xlsx:
            write_xlsx(out_xlsx, 'matriz', HEADER, out_csv)
        feedback.pushInfo(('Llamadas a OSRM: {0} | Pares: {1}').format(n_calls, len(rows)))
        return {'OUTPUT_CSV': out_csv, 'OUTPUT_XLSX': out_xlsx}
