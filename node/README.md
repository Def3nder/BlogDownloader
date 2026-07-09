# BlogDownloader — Node.js-Version

Node.js-Portierung von `../scrape_all.py`. Scrapt drei Quellen als Ablaufplan
**nacheinander** und speichert pro Beitrag eine Markdown-Datei (Blog und Facebook
zusätzlich das Titel-/Hauptbild) in Jahresordner:

1. **Blog** — `joeturan.com/blog` → `www/Joe Turan/`
2. **Facebook** — login-pflichtige Seite (via `cookies.txt`) → `www/Facebook/`
3. **Telegram** — `t.me/s/<channel>` → `www/Telegram/`

Jede Quelle bricht ab, sobald **5** bereits vorhandene Artikel erkannt werden
(`SKIP_LIMIT`), sodass wiederholte Läufe inkrementell bleiben. Beiträge mit
„Kuschel Workshop" werden übersprungen.

> Dieses Verzeichnis ist **eigenständig**: Cookies, URL-Datei und Ausgabeordner
> liegen standardmäßig hier in `node/`, getrennt von der Python-Version.

## Voraussetzungen

- Node.js **>= 18**
- Einmalige Installation:

```bash
cd node
npm install
npx playwright install chromium
```

## Ausführung

```bash
# Alle drei Quellen (Blog -> Facebook -> Telegram)
node scrape_all.js

# Einzelne Quellen (Flags kombinierbar)
node scrape_all.js --telegram
node scrape_all.js --blog --facebook

# Browser sichtbar (Debugging)
node scrape_all.js --visible

# Hilfe / alle Optionen
node scrape_all.js --help
```

### Facebook-Eingaben

Für die Facebook-Quelle im `node/`-Verzeichnis ablegen (oder per CLI überschreiben):

- `cookies.txt` — Login-Cookies im **Netscape-Format**
- `Abonenten-URL.txt` — die zu scrapende Facebook-URL

Alternativ auf die Dateien der Python-Version zeigen:

```bash
node scrape_all.js --facebook --cookies ../cookies.txt --url-file ../Abonenten-URL.txt
```

## CLI-Optionen

| Option | Zweck |
|---|---|
| `--blog` / `--facebook` / `--telegram` | Quelle(n) auswählen (ohne Flag: alle drei) |
| `--visible` | Browserfenster anzeigen |
| `--base-url <url>` | Blog-Startseite (Default `joeturan.com/blog`) |
| `--facebook-url <url>` | Facebook-URL (sonst `Abonenten-URL.txt`) |
| `--channel <name>` | Telegram-Channel (Default `joeturan`) |
| `--cookies <path>` | Pfad zu `cookies.txt` |
| `--url-file <path>` | Pfad zu `Abonenten-URL.txt` |
| `--blog-output` / `--facebook-output` / `--telegram-output` `<path>` | Zielordner überschreiben |

Bei einem Fehler in einer Quelle wird protokolliert und mit der nächsten Quelle
fortgefahren; am Ende folgt eine Zusammenfassung je Quelle. Log: `scrape_all.log`.

## Unterschied zur Python-Version

Die Markdown-Konvertierung nutzt `turndown` (statt `markdownify`); die Ausgabe
ist funktional gleich, aber nicht zeichengenau identisch (Aufzähl-/Betonungs-
zeichen können abweichen). Bildkonvertierung erfolgt über `sharp` (statt Pillow).
