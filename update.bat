@echo off
rem Haalt de nieuwste versie van de bot op. Je .env, client_secret.json en de tokens blijven staan.
cd /d "%~dp0"
set BASE=https://raw.githubusercontent.com/RobinYK44/youtube/claude/youtube-shorts-streamers-b2gowb
rem ?v=... makes GitHub send the newest files instead of a copy from a few minutes ago.
set V=%RANDOM%%RANDOM%
echo Nieuwste versie ophalen...
curl -fsSL -o files.txt "%BASE%/files.txt?v=%V%" || (echo Downloaden mislukt. Heb je internet? & pause & exit /b 1)
for /f "usebackq delims=" %%F in ("files.txt") do (
    curl -fsSL -o "%%F" "%BASE%/%%F?v=%V%" || (echo Downloaden van %%F mislukt. & pause & exit /b 1)
)
echo Onderdelen bijwerken...
python -m pip install -q -r requirements.txt
rem YouTube verandert vaak iets: altijd de nieuwste yt-dlp.
python -m pip install -q -U yt-dlp
rem Spraakherkenning voor de ondertitels. Lukt dit niet, dan maakt de bot shorts zonder ondertitels.
python -m pip install -q faster-whisper || echo Ondertitels installeren lukte niet, de bot werkt verder zonder.
curl -fsSL -o update.new "%BASE%/update.bat?v=%V%"
echo.
echo Klaar! Dubbelklik nu op start.bat om de bot te starten.
pause
if exist update.new move /y update.new update.bat >nul & exit /b
