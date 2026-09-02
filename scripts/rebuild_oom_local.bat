@echo off
REM Lokální rebuild .omap – Python 3.13 + pyogrio (bez OSGeo4W PYTHONHOME)
set PYTHONHOME=
set PYTHONPATH=
set PROJ_LIB=
set PROJ_DATA=
"C:\Program Files\Python313\python.exe" "%~dp0rebuild_oom.py" %*
