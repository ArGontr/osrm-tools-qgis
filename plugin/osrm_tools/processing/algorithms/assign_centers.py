# -*- coding: utf-8 -*-
import os
from concurrent.futures import ThreadPoolExecutor, as_completed

from qgis.core import (
    QgsProcessing,
    QgsProcessingParameterBoolean,
    QgsProcessingParameterField,
    QgsProcessingParameterFileDestination,
    QgsProcessingParameterNumber,
    QgsProcessingParameterVectorLayer,
    QgsPointXY,
    QgsSpatialIndex,
)

from ..base import OsrmAlgorithmBase, haversine_km
from ..writers import append_csv, read_ids, write_xlsx

HEADER = ['id_demanda', 'id_centro', 'minutos', 'km', 'km_euclid',
          'n_candidatos', 'radio_superado', 'estado']


class AssignCenters(OsrmAlgorithmBase):

    def name(self):
        return 'assign_centers'

    def displayName(self):  # noqa: N802
        return ('Asignar demanda a centros de servicio (masivo)')

    def group(self):
        return ('Matrices y asignacion')

    def groupId(self):  # noqa: N802
        return 'assignment'

    def shortHelpString(self):  # noqa: N802
        return (
            'Asigna cada elemento de la capa de demanda (puntos, AGEBs o manzanas; '
            'para poligonos usa un punto garantizado dentro) al centro de servicio '
            'mas cercano POR RED VIAL (OSRM table). Proceso: (1) pre-filtro '
            'euclidiano con los K centros mas cercanos, (2) bloques de demanda '
            'vecina con tabla OSRM por bloque, (3) minimo tiempo/distancia de red. '
            'Escala pensada: cientos de miles o millones de demandas contra miles '
            'de centros. Escribe resultados por bloques (checkpoint) y permite '
            'reanudar si se interrumpe.')

    def createInstance(self):  # noqa: N802
        return AssignCenters()

    def initAlgorithm(self, config=None):  # noqa: N802
        self.addParameter(QgsProcessingParameterVectorLayer(
            'DEMAND', ('Capa de demanda (puntos o poligonos)'),
            types=[QgsProcessing.TypeVectorPoint, QgsProcessing.TypeVectorPolygon]))
        self.addParameter(QgsProcessingParameterField(
            'DEMAND_ID', ('Campo ID en demanda'),
            parentLayerParameterName='DEMAND'))
        self.addParameter(QgsProcessingParameterVectorLayer(
            'CENTERS', ('Capa de centros de servicio (puntos)'),
            types=[QgsProcessing.TypeVectorPoint]))
        self.addParameter(QgsProcessingParameterField(
            'CENTER_ID', ('Campo ID en centros'),
            parentLayerParameterName='CENTERS'))
        self.addParameter(QgsProcessingParameterNumber(
            'K', ('Candidatos por demanda (K vecinos euclidianos)'),
            type=QgsProcessingParameterNumber.Integer, defaultValue=10,
            minValue=1, maxValue=99))
        self.addParameter(QgsProcessingParameterNumber(
            'MAX_RADIUS_KM', ('Radio euclidiano max. (km; 0 = sin limite)'),
            type=QgsProcessingParameterNumber.Double, defaultValue=0.0,
            minValue=0.0))
        self.addParameter(QgsProcessingParameterNumber(
            'CHUNK', ('Demandas por bloque'),
            type=QgsProcessingParameterNumber.Integer, defaultValue=750,
            minValue=10, maxValue=5000))
        self.addParameter(QgsProcessingParameterNumber(
            'THREADS', ('Hilos paralelos'),
            type=QgsProcessingParameterNumber.Integer, defaultValue=4,
            minValue=1, maxValue=8))
        self.addParameter(QgsProcessingParameterNumber(
            'MAX_LOCS', ('Max. localidades por llamada al servidor'),
            type=QgsProcessingParameterNumber.Integer, defaultValue=9000,
            minValue=100))
        self.addParameter(QgsProcessingParameterBoolean(
            'CHECKPOINT', ('Reanudable (guarda avance por bloques)'),
            defaultValue=True))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT_CSV', ('Asignaciones (CSV)'), fileFilter='CSV (*.csv)'))
        self.addParameter(QgsProcessingParameterFileDestination(
            'OUTPUT_XLSX', ('Asignaciones (XLSX, opcional)'),
            optional=True, fileFilter='XLSX (*.xlsx)'))
        self.add_server_parameters()

    def processAlgorithm(self, parameters, context, feedback):  # noqa: N802
        demand_layer = self.parameterAsVectorLayer(parameters, 'DEMAND', context)
        centers_layer = self.parameterAsVectorLayer(parameters, 'CENTERS', context)
        did = self.parameterAsString(parameters, 'DEMAND_ID', context)
        cid = self.parameterAsString(parameters, 'CENTER_ID', context)
        k = self.parameterAsInt(parameters, 'K', context)
        radius_km = self.parameterAsDouble(parameters, 'MAX_RADIUS_KM', context)
        chunk_size = self.parameterAsInt(parameters, 'CHUNK', context)
        threads = self.parameterAsInt(parameters, 'THREADS', context)
        max_locs = self.parameterAsInt(parameters, 'MAX_LOCS', context)
        checkpoint = self.parameterAsBoolean(parameters, 'CHECKPOINT', context)
        out_csv = self.parameterAsFileOutput(parameters, 'OUTPUT_CSV', context)
        out_xlsx = self.parameterAsFileOutput(parameters, 'OUTPUT_XLSX', context)
        url, profile = self.resolve_server(parameters, context)
        da = self.distance_area(context)

        centers = {}
        for f in centers_layer.getFeatures():
            p = self.representative_point(f.geometry())
            # Solo floats hacia los hilos (los objetos sip/Qt no son
            # thread-safe dentro del hilo-tarea de QGIS).
            centers[f.id()] = (str(f[cid]), p.x(), p.y())
        if not centers:
            raise ValueError(('La capa de centros no tiene elementos'))
        index = QgsSpatialIndex(centers_layer.getFeatures())
        feedback.pushInfo(('Centros: {0}').format(len(centers)))

        # Pre-filtro euclidiano por demanda
        feedback.setProgressText(('Pre-filtro euclidiano...'))
        demand = []
        for f in demand_layer.getFeatures():
            pt = self.representative_point(f.geometry())
            cand = index.nearestNeighbor(pt, k)
            flag = 'NO'
            if radius_km > 0:
                dentro = [c for c in cand
                          if da.measureLine(pt, QgsPointXY(centers[c][1], centers[c][2])) / 1000.0 <= radius_km]
                if dentro:
                    cand = dentro
                else:
                    cand = cand[:1]
                    flag = 'SI'
            demand.append((str(f[did]), pt.x(), pt.y(), cand, flag))
        feedback.pushInfo(('Demanda total: {0}').format(len(demand)))

        # Reanudacion desde checkpoint
        partial = out_csv + '.parcial.csv'
        done_ids = read_ids(partial) if (checkpoint and os.path.exists(partial)) else set()
        if done_ids:
            feedback.pushInfo(('Reanudando: {0} ya procesadas').format(len(done_ids)))
        pending = [d for d in demand if d[0] not in done_ids]

        # Armar bloques cuyos candidatos quepan en una llamada al servidor
        chunks = []
        cur = []
        union = set()
        for item in pending:
            add = set(item[2]) - union
            if cur and (len(union) + len(add) + len(cur) + 1 > max_locs):
                chunks.append(cur)
                cur = []
                union = set()
                add = set(item[2])
            cur.append(item)
            union |= add
        if cur:
            chunks.append(cur)
        feedback.pushInfo(('Bloques a procesar: {0}').format(len(chunks)))

        def worker(chunk):
            union_ids = []
            pos = {}
            for _, _, _, cands, _ in chunk:
                for c in cands:
                    if c not in pos:
                        pos[c] = len(union_ids)
                        union_ids.append(c)
            sources = [(centers[c][1], centers[c][2]) for c in union_ids]
            dests = [(x, y) for _, x, y, _, _ in chunk]
            try:
                matrix = table(url, profile, sources, dests)
            except Exception as e:  # noqa: BLE001
                # Nada de excepciones cruzando hacia los hilos de QGIS
                return [(d, '', '', '', '', len(c), fl, 'error: {0}'.format(e))
                        for d, _, _, c, fl in chunk]
            rows = []
            for i, (dem_id, x, y, cands, flag) in enumerate(chunk):
                best = None
                for c in cands:
                    dur, dist = matrix[pos[c]][i]
                    if dur is None:
                        continue
                    if best is None or dur < best[0]:
                        best = (dur, dist, c)
                if best is None:
                    rows.append((dem_id, '', '', '', '', len(cands), flag, 'sin_ruta'))
                else:
                    euclid = haversine_km(x, y, centers[best[2]][1], centers[best[2]][2])
                    rows.append((dem_id, centers[best[2]][0],
                                 round(best[0] / 60.0, 2), round(best[1] / 1000.0, 3),
                                 round(euclid, 3), len(cands), flag, 'OK'))
            return rows

        processed = 0
        canceled = False
        with ThreadPoolExecutor(max_workers=max(1, min(threads, 8))) as ex:
            futures = {ex.submit(worker, c): len(c) for c in chunks}
            for fut in as_completed(futures):
                if feedback.isCanceled():
                    canceled = True
                    for f in futures:
                        f.cancel()
                    break
                rows = fut.result()
                append_csv(partial, HEADER, rows)
                processed += futures[fut]
                feedback.setProgress(processed / max(1, len(pending)) * 100.0)

        if canceled:
            feedback.reportError((
                'Cancelado. Avance guardado en {0} (vuelve a correr para reanudar).').format(partial))
            return {}

        os.replace(partial, out_csv)
        if out_xlsx:
            if not write_xlsx(out_xlsx, 'asignaciones', HEADER, out_csv):
                feedback.pushWarning(('openpyxl no disponible; se omite XLSX'))

        total_rows = len(pending) + len(done_ids)
        feedback.pushInfo(('Demandas asignadas: {0}/{1}').format(processed, total_rows))
        return {'OUTPUT_CSV': out_csv, 'OUTPUT_XLSX': out_xlsx}
