# -*- coding: utf-8 -*-
import os

from qgis.core import QgsProcessingProvider
from qgis.PyQt.QtGui import QIcon

from .algorithms.route_one_to_n import RouteOneToN
from .algorithms.matrix_mxn import MatrixMXN
from .algorithms.assign_centers import AssignCenters
from .algorithms.routes_assigned import RoutesAssigned

ICON_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'icon.svg')


class OSRMProvider(QgsProcessingProvider):

    def loadAlgorithms(self, *args, **kwargs):  # noqa: N802 (API de QGIS)
        for alg in (RouteOneToN(), MatrixMXN(), AssignCenters(), RoutesAssigned()):
            self.addAlgorithm(alg)

    def id(self, *args, **kwargs):  # noqa: A003
        return 'osrm_tools'

    def name(self, *args, **kwargs):  # noqa: A003
        return ('OSRM Tools')

    def longName(self, *args, **kwargs):  # noqa: N802
        return self.name()

    def icon(self):
        return QIcon(ICON_PATH)
