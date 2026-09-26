@echo off
rem Log een keer in bij TikTok, zodat de bot zelf op TikTok kan posten.
cd /d "%~dp0"
python -m shortsbot.tiktok auth
echo.
pause
