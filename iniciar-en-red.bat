@echo off
rem Igual que iniciar.bat, pero accesible desde otros equipos de tu red
rem (por ejemplo, generar en el portatil y usarlo desde el sobremesa).
call "%~dp0iniciar.bat" --red %*
