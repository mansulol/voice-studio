@echo off
chcp 65001 >nul
title Estudio de voz
cd /d "%~dp0"

rem ── 1. Si el entorno es de otro ordenador (carpeta copiada) o esta roto, se rehace ──
if exist ".venv\Scripts\python.exe" (
    ".venv\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo  El entorno .venv es de otro ordenador o esta roto: lo vuelvo a crear.
        rmdir /s /q ".venv"
    )
)
if exist ".venv-whisper\Scripts\python.exe" (
    ".venv-whisper\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo  El entorno .venv-whisper es de otro ordenador o esta roto: lo vuelvo a crear.
        rmdir /s /q ".venv-whisper"
    )
)
if exist ".venv-kokoro\Scripts\python.exe" (
    ".venv-kokoro\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo  El entorno .venv-kokoro es de otro ordenador o esta roto: lo borro ^(vuelve a ejecutar instalar-voces.bat^).
        rmdir /s /q ".venv-kokoro"
    )
)
if exist ".venv-qwen\Scripts\python.exe" (
    ".venv-qwen\Scripts\python.exe" -c "import sys" >nul 2>&1
    if errorlevel 1 (
        echo  El entorno .venv-qwen es de otro ordenador o esta roto: lo borro ^(vuelve a ejecutar instalar-voces.bat^).
        rmdir /s /q ".venv-qwen"
    )
)

rem ── 2. Crear el entorno si no existe (busca Python 3.11, 3.12 o 3.10) ──
if not exist ".venv\Scripts\python.exe" (
    set "PY="
    py -3.11 -c "import sys" >nul 2>&1 && set "PY=py -3.11"
    if not defined PY py -3.12 -c "import sys" >nul 2>&1 && set "PY=py -3.12"
    if not defined PY py -3.10 -c "import sys" >nul 2>&1 && set "PY=py -3.10"
    if not defined PY python -c "import sys; sys.exit(0 if (3,10) <= sys.version_info[:2] <= (3,12) else 1)" >nul 2>&1 && set "PY=python"
    if not defined PY goto :sinpython
    echo.
    echo  Primera vez: preparando todo ^(voz y transcripcion^).
    echo  Tarda bastantes minutos y descarga varios GB. Solo pasa una vez.
    echo.
    call %%PY%% -m venv .venv
    if errorlevel 1 goto :sinpython
)

rem ── 3. Instalar (o terminar una instalacion que se corto a medias) ──
".venv\Scripts\python.exe" -c "import chatterbox" >nul 2>&1
if errorlevel 1 (
    ".venv\Scripts\python.exe" instalar.py
    if errorlevel 1 (
        echo.
        echo  [!] La instalacion ha fallado. Revisa los mensajes de arriba.
        pause
        exit /b 1
    )
)

".venv\Scripts\python.exe" servidor.py %*
pause
exit /b 0

:sinpython
echo.
echo  [!] Necesito Python 3.11 ^(sirven 3.10 a 3.12^) y no lo encuentro.
echo      Instalalo con uno de estos comandos y vuelve a abrir este archivo:
echo        py install 3.11
echo        winget install Python.Python.3.11
echo.
echo      Para ver que versiones tienes:  py list   ^(o  py -0^)
pause
exit /b 1
