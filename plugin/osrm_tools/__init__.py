# -*- coding: utf-8 -*-
# Copyright (C) 2026 ArGontr (https://github.com/ArGontr)
# SPDX-License-Identifier: GPL-3.0-or-later
"""OSRM Tools - Plugin de Processing para analisis de accesibilidad con OSRM."""


def classFactory(iface):  # pylint: disable=C0103
    from .osrm_tools_plugin import OSRMToolsPlugin
    return OSRMToolsPlugin(iface)
