@echo off
chcp 65001 >nul
title Instalar la pestaña Voces (Kokoro y Qwen3-TTS)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo  Primero abre iniciar.bat una vez para preparar el entorno principal.
    pause
    exit /b 1
)
echo.
echo  Instala Kokoro (voces en ingles, ~1 GB) y Qwen3-TTS (disenar voces, ~3 GB).
echo  Los modelos se descargan la primera vez que los uses (Qwen: unos 4,5 GB mas).
echo.
".venv\Scripts\python.exe" instalar.py --solo-voces
pause
