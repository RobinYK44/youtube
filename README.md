# Shorts-bot 🎬

Een Discord-bot die **zelf** YouTube Shorts maakt en uploadt van de populairste Twitch-streamers.

Wat de bot doet:

1. Zodra hij aan staat, maakt hij de shorts voor de komende 24 uur. Hij zoekt de **meest bekeken clips** van de afgelopen 2 dagen, van je vaste streamers (Jynxzi, Kai Cenat, CaseOh, xQc, ...) **plus** de 5 grootste streamers die op dat moment live zijn.
2. Hij pakt de clip met de hoogste **viral-score** die nog niet gebruikt is (en niet 2× achter elkaar dezelfde streamer). De score kijkt naar hoe snel de views binnenkomen, of de clip uitschiet voor die streamer, woorden in de titel ("LMAO", "WTF", "no way", 💀 ...) en de lengte.
3. Hij maakt er een **verticale 9:16-video** van: vage achtergrond, clip in het midden, titel bovenin, `twitch.tv/naam` onderin.
4. Hij **uploadt** hem naar je YouTube-kanaal met titel, #shorts, tags en credits, ingepland op een vaste tijd (standaard 12:00, 16:00, 20:00 en 23:00). YouTube zet hem dan zelf online, ook als je computer uit staat.
5. Hij meldt alles in je **Discord-kanaal**.

---

## ⚠️ Eerst even eerlijk

- **IShowSpeed** streamt op YouTube, niet op Twitch. Deze bot haalt clips via de officiële Twitch-API, dus Speed zit er niet in. Jynxzi, Kai Cenat, CaseOh enz. wel.
- **Copyright:** je uploadt beelden van iemand anders. Veel streamers vinden clippen prima (het is gratis reclame), maar ze *kunnen* een claim of strike geven. Daarom zet de bot altijd de naam en link van de streamer erbij. Monetization kan YouTube weigeren als "hergebruikte content".
- **YouTube API:** een nieuw Google-project mag video's alleen **privé** uploaden, totdat je een (gratis) *audit* aanvraagt: <https://support.google.com/youtube/contact/yt_api_form>. Doe dit meteen; daarna komen ze gewoon openbaar online.
- **Max ~6 uploads per dag** (limiet van YouTube's API). Standaard staat hij op 4.
- De bot moet **24/7 aan staan** op een computer of server (zie stap 6).

---

## Eenmalige setup (± 20 minuten)

Je hoeft dit maar één keer te doen. Daarna doet de bot alles zelf.

### 1. Discord-bot maken
1. Ga naar <https://discord.com/developers/applications> → **New Application**.
2. Links **Bot** → **Reset Token** → kopieer de token → dat is `DISCORD_TOKEN`.
3. Links **OAuth2 → URL Generator**: vink `bot` en `applications.commands` aan, en bij permissies `Send Messages` + `Embed Links`. Open de link en voeg de bot toe aan je server.
4. In Discord: Instellingen → Geavanceerd → **Ontwikkelaarsmodus** aan. Rechtermuisknop op het kanaal waar de bot moet praten → **Kanaal-ID kopiëren** → dat is `DISCORD_CHANNEL_ID`.

### 2. Twitch-app maken
1. Ga naar <https://dev.twitch.tv/console/apps> → **Register Your Application**.
2. Naam: iets willekeurigs, OAuth Redirect URL: `http://localhost`, Category: `Other`, Client Type: `Confidential`.
3. Kopieer **Client ID** en maak een **Client Secret** → `TWITCH_CLIENT_ID` en `TWITCH_CLIENT_SECRET`.

### 3. YouTube-toegang
1. Ga naar <https://console.cloud.google.com/> en maak een nieuw project.
2. **APIs & Services → Library** → zoek **YouTube Data API v3** → **Enable**.
3. **OAuth consent screen**: kies *External*, vul naam en e-mail in, voeg jezelf toe als test user. Zet daarna **Publishing status op "In production"** (anders moet je elke 7 dagen opnieuw inloggen).
4. **Credentials → Create credentials → OAuth client ID** → type **Desktop app** → download de JSON en zet hem in deze map als `client_secret.json`.
5. Vraag de audit aan (zie hierboven) zodat uploads openbaar mogen.

### 4. Installeren
Je hebt **Python 3.10+** nodig (<https://python.org>). Voor tekst in beeld is een volledige **ffmpeg** handig (Windows: `winget install ffmpeg`, Mac: `brew install ffmpeg`). Zonder ffmpeg werkt het ook, maar dan zonder titel in de video.

```bash
pip install -r requirements.txt
cp .env.example .env        # Windows: copy .env.example .env
```

Open `.env` en vul de gegevens van stap 1 en 2 in.

### 5. Inloggen bij YouTube (op een pc met browser)
```bash
python -m shortsbot.youtube auth
```
Er opent een browser: log in met het account van je YouTube-kanaal. Er komt een bestand `youtube_token.json` bij.

### 6. Starten
```bash
python main.py
```
De bot zegt hallo in je Discord-kanaal en maakt vanaf nu automatisch shorts. 🎉

**24/7 laten draaien:** laat je pc aanstaan, of zet het op een goedkope server (VPS, ± €4/maand bij bijv. Hetzner). Met Docker:
```bash
docker build -t shortsbot .
docker run -d --restart always --name shortsbot \
  --env-file .env \
  -v "$PWD/youtube_token.json:/app/youtube_token.json" \
  -v "$PWD/client_secret.json:/app/client_secret.json" \
  -v "$PWD/data:/app/data" shortsbot
```

---

## Discord-commando's

| Commando | Wat het doet |
|---|---|
| `/status` | Hoeveel shorts vandaag, wanneer de volgende komt, laatste uploads |
| `/nu` | Meteen een short maken en uploaden |
| `/kiesmodus aantal` | Elke dag zoveel shorts maken waar jij uit kiest (0 = volledig automatisch) |
| `/meer aantal` | Kiesmodus: nu meteen extra shorts zoeken om uit te kiezen (standaard 10) |
| `/top` | De 10 grootste live streamers op dit moment |
| `/streamers` | Van welke streamers de bot clips zoekt |
| `/streamer_toevoegen naam` | Streamer toevoegen (Twitch-naam) |
| `/streamer_verwijderen naam` | Streamer weghalen |
| `/pauze` / `/hervat` | Tijdelijk stoppen / weer verder |
| `/ingepland_wissen` | Tijden van ingeplande shorts weer vrijmaken (verwijder de video's zelf in YouTube Studio) |

### Kiesmodus: zelf de beste kiezen
Met `/kiesmodus aantal:15` maakt de bot elke dag 15 shorts en stuurt ze met een voorbeeldvideo naar Discord.
Klik op **✅ Kies deze** bij de shorts die je het beste vindt; ze worden ingepland op de eerstvolgende vrije tijd.
Met **❌ Afkeuren** gooi je een short weg; die wordt nooit gebruikt, ook niet als de bot zelf kiest.
Kies je niet op tijd, dan kiest de bot 45 minuten van tevoren zelf de short met de meeste views.
`/kiesmodus aantal:0` zet hem weer op volledig automatisch.

## Instellingen (`.env`)

| Instelling | Standaard | Uitleg |
|---|---|---|
| `STREAMERS` | jynxzi, kaicenat, ... | Vaste lijst Twitch-namen |
| `AUTO_DISCOVER_TOP` | 5 | Ook de N grootste live streamers meenemen |
| `DISCOVER_LANGUAGE` | en | Taal van die live streamers (`nl` voor Nederlands) |
| `PUBLISH_TIMES` | 12:00,16:00,20:00,23:00 | Tijden waarop de shorts online komen (max 6 per dag) |
| `TIMEZONE` | Europe/Amsterdam | Tijdzone van die tijden |
| `CANDIDATES_PER_DAY` | 0 | Kiesmodus: zoveel shorts per dag maken om uit te kiezen (ook via `/kiesmodus`) |
| `YOUTUBE_PRIVACY` | public | `public`, `unlisted` of `private` |
| `CLIP_LOOKBACK_DAYS` | 2 | Hoe ver terug zoeken naar clips |
| `MIN_CLIP_VIEWS` | 500 | Alleen clips met minstens zoveel views |
| `MIN_CLIP_SECONDS` | 10 | Alleen clips van minstens zoveel seconden |
| `MAX_SHORT_SECONDS` | 60 | Maximale lengte van de short |
