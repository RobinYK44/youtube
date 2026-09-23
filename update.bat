@echo off
rem Haalt de nieuwste versie van de bot op. Je .env, client_secret.json en youtube_token.json blijven staan.
cd /d "%~dp0"
set BASE=https://raw.githubusercontent.com/RobinYK44/youtube/claude/youtube-shorts-streamers-b2gowb
echo Nieuwste versie ophalen...
for %%F in (main.py requirements.txt start.bat shortsbot/__init__.py shortsbot/bot.py shortsbot/config.py shortsbot/db.py shortsbot/editor.py shortsbot/pipeline.py shortsbot/twitch.py shortsbot/youtube.py) do (
    curl -fsSL -o "%%F" "%BASE%/%%F" || (echo Downloaden van %%F mislukt. & pause & exit /b 1)
)
echo Onderdelen bijwerken...
python -m pip install -q -r requirements.txt
echo.
echo Klaar! Dubbelklik nu op start.bat om de bot te starten.
pause
