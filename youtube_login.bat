@echo off
rem Log opnieuw in bij YouTube (nodig als de bot erom vraagt, bijvoorbeeld om je views te lezen).
cd /d "%~dp0"
python -m shortsbot.youtube auth
echo.
echo Start de bot daarna opnieuw met start.bat.
pause
