# BlogDownloader — `scrape_all.py`

Konsolidierter Scraper, der die Inhalte des Autors **„Joe Turan"** aus drei Quellen
archiviert: **Blog**, **Facebook** und **Telegram**. Pro Beitrag entsteht eine
Markdown-Datei (Blog und Facebook zusätzlich das Titel-/Hauptbild) in
jahresbasierten Ordnern. Alles ist deutschsprachig und stark auf die Ausgabe
dieses konkreten Autors zugeschnitten.

`scrape_all.py` ist der **einzige Einstiegspunkt**. Es gibt auch eine
funktionsgleiche Node.js-Portierung unter `node/` (siehe unten).

---

## Schnellstart

```bash
# Einmalige Einrichtung
pip install -r requirements.txt
playwright install chromium          # Pflicht: Playwright steuert echtes Chromium

# Vollständiger Lauf (Blog -> Facebook -> Telegram, in dieser Reihenfolge)
python scrape_all.py

# Teilmenge (Flags kombinierbar; ohne Flag laufen alle drei)
python scrape_all.py --telegram
python scrape_all.py --blog --facebook
python scrape_all.py --visible       # Browserfenster sichtbar

# Schneller Syntax-Check (es gibt keine Testsuite)
python -m py_compile scrape_all.py
```

---

## Was das Script tut

`run_all()` öffnet **einen** asynchronen Playwright-Chromium-Browser und führt die
ausgewählten Quellen **nacheinander** aus. Jede Quelle:

1. bekommt einen eigenen Browser-`context` (Facebook zusätzlich Cookies +
   `de-DE`-Locale + hohes Viewport),
2. scrollt/paginiert durch die Beiträge,
3. speichert pro Beitrag eine Markdown-Datei (+ ggf. Bild) im Jahresordner,
4. **bricht ab, sobald `SKIP_LIMIT` bereits vorhandene Beiträge erkannt werden**
   (so bleiben erneute Läufe inkrementell statt alles neu zu laden),
5. gibt `(gespeichert, übersprungen)` zurück.

**Fehler-Toleranz:** Scheitert eine Quelle, wird der Fehler mit Traceback
protokolliert und mit der nächsten Quelle **weitergemacht**; am Ende steht eine
Zusammenfassung je Quelle. Der Exit-Code ist ≠ 0, wenn mindestens eine Quelle
fehlschlug (die anderen liefen trotzdem).

Beiträge mit **„Kuschel Workshop"** (`EXCLUDE_RE`) werden übersprungen.

---

## Die drei Quellen

| Quelle | Ausgabeordner | Dateiname | Slug-Quelle | Inhalts-Extraktion |
|---|---|---|---|---|
| **Blog** (`scrape_blog`) | `Joe_Turan_Archiv/<Jahr>/` | `<Datum>_<slug>.md` + `.jpg` | letztes URL-Segment ab dem ersten `_` | `bs4`-Meta-Tags; `div.jw-element-imagetext-text` → `markdownify` |
| **Facebook** (`scrape_facebook`) | `Joe_Turan_Facebook/<Jahr>/` | `<Datum>_Joe Turan - <slug>.md` + Bild | erster Satz des Textes | längster `dir=auto`-Textblock (Klartext) |
| **Telegram** (`scrape_telegram`) | `Joe_Turan_Telegram/<Jahr>/` | `<Datum>_Joe Turan - <slug>.md` | erster Satz des Textes | `.tgme_widget_message_text` |

> **Wichtige Invarianten:** Dateinamen-Schema, Ausgabeordner und Markdown-Format
> pro Quelle sind *load-bearing* — die „existiert bereits"-Prüfung vergleicht
> gegen genau diese Pfade. Ändert man ein Namens-/Formatschema, wird das ganze
> Archiv neu heruntergeladen.

### Quellen-Besonderheiten

- **Blog** — bezieht **alle** Metadaten ausschließlich aus den Head-Meta-Tags
  (`og:title` / `og:url` / `og:image`, `itemprop=datePublished`), nie aus dem
  Fließtext; paginiert über `data-page-next`. Das Titelbild wird über Playwrights
  Request-API geladen und mit **Pillow** zu einem RGB-JPEG normalisiert.
- **Facebook** — benötigt `cookies.txt` (Netscape-Format) für die eingeloggte
  Sitzung und liest die Seiten-URL aus `Abonenten-URL.txt` (sofern nicht
  `--facebook-url`). Statt jeden Beitrag anzuklicken, **erntet es passiv** den
  echten Permalink + `creation_time` aus dem JSON, das FB ohnehin ausliefert
  (eingebettetes `<script type="application/json">` im initialen HTML plus
  mitgeschnittene `/graphql`-Antworten), verschlüsselt über einen normalisierten
  Text-Präfix (`lookup_post_meta`). DOM-Scraping von Datum/Permalink ist nur
  Fallback. Der Feed ist virtualisiert → schrittweises Scrollen, Dedup über das
  stabile `aria-posinset`.
- **Telegram** — nutzt die öffentliche Web-Ansicht `t.me/s/<channel>` (Default
  `joeturan`) und verarbeitet Beiträge **von unten nach oben** (neuester zuerst,
  dann in die Vergangenheit).

---

## Gemeinsame Text-Bereinigung

Pro Beitrag angewandt:

1. `strip_leading_intro` / `strip_trailing_greeting` — entfernen einleitende
   bzw. abschließende Floskeln/Grußzeilen (z. B. „Impuls des Tages:",
   „Schönen Abend noch 🤍").
2. **CTA-Entfernung** — zwei Varianten, die sich `_CTA_TRIGGERS` teilen:
   `strip_cta_block_paragraphs` (Blog: Absätze durch Leerzeilen getrennt) vs.
   `strip_cta_block_lines` (Facebook: jeder Absatz eine `\n`-Zeile).
   `_CTA_TRIGGERS` ist eine wachsende Liste von Werbe-Phrasen
   (String = Teilstring-Treffer; Tupel = **alle** Teilstrings nötig) — wird bei
   Bedarf erweitert.
3. `strip_signature` — kollabiert den abschließenden Autoren-/Social-Media-Abspann
   auf ein einzelnes `Joe Turan`. Ankerstrategie: unscharfer „Joe Turan"-Treffer
   (difflib-Ratio ≥ 0,75), gefolgt von `joeturan.com` innerhalb der nächsten 3
   Zeilen.

Facebook zusätzlich: `strip_fb_ui_text` entfernt „… Mehr/Weniger anzeigen".

---

## Markdown-Format

**Blog** (`render_blog_markdown`):
```markdown
# Titel

*Quelle: https://…*

**Datum: 2026-01-25**

<Artikeltext>
```

**Telegram / Facebook** (`render_post_markdown`, mit `---`-Trenner):
```markdown
# Titel

*Quelle: https://…*

Datum: 2026-01-25

---
<Beitragstext>
```

Der Titel ist der **erste Satz** des Beitrags (Telegram/FB) bzw. `og:title` (Blog);
der Slug im Dateinamen wird über `slugify` erzeugt (Sonderzeichen → `_`, auf
`SLUG_MAX_LEN`=55 an Wortgrenzen gekürzt).

---

## CLI-Optionen

| Option | Zweck |
|---|---|
| `--blog` / `--facebook` / `--telegram` | Quelle(n) auswählen (ohne Flag: alle drei, Reihenfolge Blog→Facebook→Telegram) |
| `--visible` | Browserfenster anzeigen (sonst headless) |
| `--base-url <url>` | Blog-Startseite (Default `https://www.joeturan.com/blog`) |
| `--facebook-url <url>` | Facebook-URL (sonst `Abonenten-URL.txt`) |
| `--channel <name>` | Telegram-Channel (Default `joeturan`) |
| `--cookies <pfad>` | Pfad zu `cookies.txt` |
| `--url-file <pfad>` | Pfad zu `Abonenten-URL.txt` |
| `--blog-output` / `--facebook-output` / `--telegram-output` `<pfad>` | Zielordner überschreiben |

---

## Konfiguration (oben im Script)

- `SKIP_LIMIT` — Abbruchschwelle je Quelle (aktuell **3** bereits vorhandene
  Beiträge). Steuert, wie „tief" ein Re-Run zurückläuft.
- `CHANNEL_DISPLAY_NAME` = `Joe Turan`, `DEFAULT_CHANNEL` = `joeturan`,
  `DEFAULT_BASE_URL` = Blog-URL.
- Ausgabeordner: `Joe_Turan_Archiv` / `Joe_Turan_Facebook` / `Joe_Turan_Telegram`.
- Eingaben: `cookies.txt`, `Abonenten-URL.txt`. Log: `scrape_all.log`.
- `EXCLUDE_RE` (Kuschel Workshop), `_CTA_TRIGGERS` (Werbe-Phrasen).

---

## Abhängigkeiten

`requirements.txt`:

```
beautifulsoup4
markdownify
Pillow
playwright
```

Bewusst **ohne** `requests`: Bild-Downloads laufen über Playwrights
`context.request`. `markdownify` ist für den Blog unverzichtbar (formatiertes
HTML → Markdown), `bs4` für sauberes Meta-Parsing, `Pillow` für die
JPEG-Normalisierung. Playwright braucht einmalig `playwright install chromium`.

---

## Logging

Ein einzelner Logger schreibt nach **`scrape_all.log`** (und auf die Konsole),
Format `YYYY-MM-DD HH:MM:SS [LEVEL] Meldung`. Rotiert nicht. Am Ende jedes Laufs
folgt die Zusammenfassung, z. B.:

```
Zusammenfassung:
  blog       gespeichert=0  bereits vorhanden=3
  facebook   gespeichert=2  bereits vorhanden=3
  telegram   gespeichert=0  bereits vorhanden=3
```

---

## Nachbearbeitung

Unabhängige, stdlib-only Post-Prozessoren, die die pro-Jahr-Markdown-Dateien
(getrennt durch `---`) zusammenführen:

```bash
python merge_years.py <archiv-ordner>   # -> <ordner>/yearly/<ordner>-<jahr>.md
python merge_all.py   <archiv-ordner>   # -> <ordner>/<ordner>_ALL.md (braucht yearly/)
```

---

## Node.js-Portierung (`node/`)

`node/` enthält eine eigenständige, funktionsgleiche JavaScript/ESM-Portierung
(`node scrape_all.js`, gleiche CLI/Flags), gebaut mit Playwright + cheerio +
turndown + sharp + difflib. Sie ist self-contained (eigene
`cookies.txt`/`Abonenten-URL.txt`/Ausgabeordner unter `node/`). Bei Änderungen an
der Scraping-Logik beide Fassungen synchron halten; die `turndown`-Ausgabe ist
bewusst nicht zeichengenau identisch zu `markdownify`.

Eine an das Projekt **WebArchiv** angepasste Kopie (schreibt in dessen `www/`,
per Klick im Web-UI auslösbar) liegt dort unter `scraper/`.

---

## Hinweise

- Es gibt **keine Testsuite**; als Rauchtest eignet sich `--telegram` (kein Login
  nötig).
- `session-context.md` dokumentiert das Vor-Merge-Design des früheren
  eigenständigen `blogdownload.py`; die Architektur-Entscheidungen gelten weiter,
  Datei-/Abhängigkeitsdetails dort sind veraltet.
- Weitere Entwickler-Hinweise stehen in `CLAUDE.md`.
