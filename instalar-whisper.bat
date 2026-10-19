@echo off
chcp 65001 >nul
title Completar instalacion (transcripcion y traduccion)
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
    echo  Primero abre iniciar.bat una vez para preparar el entorno principal.
    pause
    exit /b 1
)
rem Instala WhisperX (.venv-whisper) y el traductor; lo que ya este instalado se salta rapido
".venv\Scripts\python.exe" instalar.py
pause
