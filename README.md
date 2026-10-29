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
- Uno o dos servidores OSRM propios (externos o administrados por el plugin).
  El servidor administrado usa `--max-table-size 10000`; el parametro
  *Max. localidades por llamada* debe ser menor (default del plugin: 9000).
- El plugin usa solo la stdlib de Python para HTTP (`urllib`); el XLSX opcional
  requiere `openpyxl` (incluido en QGIS para Windows).

## Servidor OSRM local administrado

Al activar el plugin se comprueba `http://127.0.0.1:5000`. Si ya hay un OSRM,
no se modifica. Si no, el plugin pregunta si deseas configurar uno. Tambien
puedes abrir **OSRM Tools > Servidor OSRM local…** o usar su boton de barra.

1. Elige **Docker** (recomendado) o **Binarios OSRM**.
2. Selecciona un `.osm.pbf` local o pega una URL HTTPS de Geofabrik, por ejemplo
   `https://download.geofabrik.de/north-america/mexico-latest.osm.pbf`.
3. Elige una carpeta con espacio suficiente. Los grafos pueden ocupar varias
   veces el tamaño del PBF y la extraccion puede requerir bastante RAM.
4. Pulsa **Preparar e iniciar**. El plugin ejecuta `osrm-extract`,
   `osrm-partition` y `osrm-customize`, y arranca MLD en el puerto 5000.
5. Selecciona **Auto (localhost:5000)** en los algoritmos, como antes.

El grafo se guarda con la huella SHA-256 del PBF y no se vuelve a procesar si
sigue siendo valido. **Cancelar operacion** interrumpe descargas y comandos.
Al cerrar QGIS o descargar el plugin se detiene unicamente el servidor creado
por el plugin; un servidor externo nunca se toca.

Docker se instala desde su pagina oficial (el boton del dialogo la abre) y debe
estar arrancado. Para binarios se descarga la version actual publicada por
[e-kotov/osrm-binaries](https://github.com/e-kotov/osrm-binaries), un proveedor
independiente de OSRM, y se valida SHA-256 antes de extraer. Estan disponibles
Windows x64, Linux x64/arm64 y macOS Intel/Apple Silicon. En Linux reciente se
requiere glibc compatible con el paquete; Docker es la alternativa si falla.

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
  server/
    controller.py      # menu, arranque al activar y cierre con QGIS
    dialog.py          # configuracion, progreso y cancelacion
    manager.py         # Docker/binarios, PBF, grafo y subproceso
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

## Pruebas

```bash
python -m unittest discover -s tests -v
```

Las pruebas del nucleo no necesitan QGIS ni Docker. La prueba final de interfaz
y del arranque con un PBF real debe hacerse dentro de QGIS en el equipo destino.

## Empaquetado para distribucion

Comprimir la carpeta `plugin\osrm_tools` como `osrm_tools.zip` (la carpeta debe
estar en la raiz del zip). Instalar en otro equipo: Complementos > Instalar
desde ZIP.
