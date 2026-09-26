@echo off
rem Start de bot. Laat dit venster open staan zolang de bot moet draaien.
cd /d "%~dp0"
set BASE=https://raw.githubusercontent.com/RobinYK44/youtube/claude/youtube-shorts-streamers-b2gowb
rem Een oudere update.bat haalde niet alle bestanden op: vul ze hier aan.
if not exist shortsbot\tiktok.py (
    curl -fsSL -o shortsbot\tiktok.py "%BASE%/shortsbot/tiktok.py"
    curl -fsSL -o update.bat "%BASE%/update.bat"
)
rem Spraakherkenning voor de ondertitels (eenmalig, ongeveer een minuut).
python -c "import faster_whisper" 2>nul || (echo Ondertitels installeren, even geduld... & python -m pip install -q faster-whisper)
python main.py
echo.
echo De bot is gestopt.
pause
