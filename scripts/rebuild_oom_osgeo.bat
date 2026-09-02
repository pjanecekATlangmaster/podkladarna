@echo off
REM Rebuild .omap přes OSGeo4W (GDAL/osgeo).
call "C:\OSGeo4W\bin\o4w_env.bat"
cd /d "%~dp0.."
python scripts\rebuild_oom.py %*
