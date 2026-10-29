# Instala el plugin en modo desarrollo en el perfil default de QGIS.
$ErrorActionPreference = 'Stop'
$src = Join-Path $PSScriptRoot 'plugin\osrm_tools'
$dstRoot = Join-Path $env:APPDATA 'QGIS\QGIS3\profiles\default\python\plugins'
$dst = Join-Path $dstRoot 'osrm_tools'

New-Item -ItemType Directory -Force $dstRoot | Out-Null
if (Test-Path $dst) { Remove-Item -Recurse -Force $dst }
Copy-Item -Recurse $src $dst
Write-Host "OK: copiado a $dst" -ForegroundColor Green
Write-Host "1) Reinicia QGIS (o usa Plugin Reloader)."
Write-Host "2) Activa 'OSRM Tools' en Complementos > Administrar e instalar complementos."
Write-Host "3) Los algoritmos aparecen en Procesamiento > OSRM Tools."
