@echo off
rem Geeft Claude LEES-toegang tot de cijfers van je kanaal (kan niks uploaden, aanpassen of verwijderen).
cd /d "%~dp0"
python -m shortsbot.insights login
echo.
pause
