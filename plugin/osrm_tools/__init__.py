# -*- coding: utf-8 -*-
"""OSRM Tools - Plugin de Processing para analisis de accesibilidad con OSRM."""


def classFactory(iface):  # pylint: disable=C0103
    from .osrm_tools_plugin import OSRMToolsPlugin
    return OSRMToolsPlugin(iface)
