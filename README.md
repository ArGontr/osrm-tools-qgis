# OSRM Tools — Plugin de QGIS

Plugin de Processing para analisis de accesibilidad y asignacion masiva usando
servidores **OSRM locales**. Reemplaza los scripts sueltos de enrutamiento con
algoritmos integrados en la caja de herramientas de Procesamiento de QGIS
(batch, Modelador, ejecucion en segundo plano, progreso y cancelacion).

## Algoritmos

| Algoritmo | Que hace | Servicio OSRM |
|---|---|---|
| Enrutar 1 origen a N destinos | Un origen, N destinos; tabla + capa de lineas opcional | `route` |
| Matriz de tiempos/distancias MxN | Tabla larga origen-destino con tiempos y distancias de red | `table` |
| Asignar demanda a centros de servicio (masivo) | Cada demanda (punto, AGEB, manzana) al centro mas cercano **por red**, con filtro euclidiano previo, bloques y reanudacion | `table` |
| Rutas para asignaciones existentes | Enruta pares ya asignados (geometrias + tiempos) | `route` |

## Requisitos

- QGIS >= 3.28 (desarrollado en 3.44).
- Uno o dos servidores OSRM propios (osrm-routed). Los contenedores locales ya
  usan `--max-table-size 10000`; el parametro *Max. localidades por llamada* de
  cada algoritmo debe ser menor que ese valor (default del plugin: 9000).
- El plugin usa solo la stdlib de Python para HTTP (`urllib`); el XLSX opcional
  requiere `openpyxl` (incluido en QGIS para Windows).

## Instalacion en desarrollo

1. Ejecuta `install_dev.ps1` (copia `plugin\osrm_tools` al perfil de QGIS).
2. Reinicia QGIS (o instala *Plugin Reloader* y recarga).
3. Activa **OSRM Tools** en Complementos > Administrar e instalar complementos.
4. Los algoritmos aparecen en el caja de herramientas de Procesamiento bajo
   **OSRM Tools**.

## Uso tipico (asignacion masiva)

1. Capa de centros de servicio (puntos) con campo ID.
2. Capa de demanda (puntos, o poligonos AGEB/manzana) con campo ID.
3. Algoritmo *Asignar demanda a centros de servicio*: K=10 candidatos
   euclidianos, radio opcional, bloques de 750, 4 hilos.
4. Salida CSV/XLSX: `id_demanda, id_centro, minutos, km, km_euclid,
   n_candidatos, radio_superado, estado`.
5. Si se cancela, el archivo `.parcial.csv` permite reanudar al volver a correr.

## Estructura

```
plugin/osrm_tools/
  metadata.txt
  __init__.py
  osrm_tools_plugin.py
  icon.svg
  processing/
    provider.py
    base.py            # parametros y utilidades comunes
    osrm_client.py     # cliente HTTP (table/route por GET, reintentos)
    writers.py         # CSV/XLSX, checkpoint
    algorithms/
      route_one_to_n.py
      matrix_mxn.py
      assign_centers.py
      routes_assigned.py
```

## Empaquetado para distribucion

Comprimir la carpeta `plugin\osrm_tools` como `osrm_tools.zip` (la carpeta debe
estar en la raiz del zip). Instalar en otro equipo: Complementos > Instalar
desde ZIP.
