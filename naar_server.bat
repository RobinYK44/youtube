@echo off
rem Zet je instellingen en logins op je server en start de bot daar.
rem Gebruik dit ook als je opnieuw hebt ingelogd bij YouTube of TikTok, of je .env hebt veranderd.
cd /d "%~dp0"
set /p IP=IP-adres van je server: 
set FILES=.env youtube_token.json
if exist client_secret.json set FILES=%FILES% client_secret.json
if exist tiktok_token.json set FILES=%FILES% tiktok_token.json
if exist data\shortsbot.db set FILES=%FILES% data\shortsbot.db
echo.
echo Typ 2 keer het wachtwoord van je server als daarom gevraagd wordt.
echo Je ziet niks terwijl je typt, dat is normaal. Druk daarna op Enter.
echo.
scp -o StrictHostKeyChecking=accept-new %FILES% root@%IP%:/opt/shortsbot/
ssh root@%IP% "systemctl restart shortsbot && sleep 5 && systemctl is-active shortsbot"
rem Je geschiedenis (welke clips al gebruikt zijn) staat nu op de server: niet nog een keer sturen.
if exist data\shortsbot.db ren data\shortsbot.db shortsbot-staat-op-server.db
echo.
echo Staat hierboven "active"? Dan draait de bot op je server. Start hem NIET meer op je laptop.
pause
