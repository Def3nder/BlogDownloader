# blogdownload.py — Session-Kontext

Stand: 2026-06-10

## Ziel

Das Script crawlt den Blog `https://www.joeturan.com/blog` und archiviert jeden Artikel als zwei Dateien:

- `<yyyy-mm-dd>_<slug>.md` — strukturiertes Markdown
- `<yyyy-mm-dd>_<slug>.jpg` — Titelbild (og:image)

Beide Dateien landen in `Joe_Turan_Archiv/<yyyy>/`.

---

## Architektur-Entscheidungen

### Ausgabeformat: Markdown statt PDF

Ursprünglich erzeugte das Script PDFs via `fpdf2`. Umgestellt auf Markdown + separate Bilddatei, weil Markdown durchsuchbar, weiterverarbeitbar und zukunftssicher ist. `fpdf2` und `pypdf` wurden aus den Dependencies entfernt.

### Metadaten ausschließlich aus Meta-Tags

Alle Felder kommen aus dem HTML-Head, nie aus dem Body-Text:

| Feld    | Meta-Tag                              |
|---------|---------------------------------------|
| Titel   | `og:title` (Suffix `/ Blog | www.joeturan.com` wird entfernt) |
| Datum   | `itemprop="datePublished"` (erste 10 Zeichen: `yyyy-mm-dd`) |
| URL     | `og:url`                              |
| Bild    | `og:image`                            |

### Dateiname aus og:url

`https://www.joeturan.com/blog/3189280_wenn-eine-frau-...` → letztes Pfadsegment → ab dem ersten `_` → `wenn-eine-frau-...`

Dateiname: `<datum>_<slug>.md` / `.jpg`

### Markdown-Layout

```
# Titel

*Quelle: https://...*

**Datum: yyyy-mm-dd**

<Artikeltext>

Joe Turan
```

- Zusammenfassung (og:description) wird **nicht** ausgegeben — sie ist immer der erste Absatz des Artikeltexts.
- Kein Bild-Link in der `.md` — Bild liegt nur als separate `.jpg` daneben.

### Inhalt: `div.jw-element-imagetext-text`

Alle Vorkommen dieses Containers werden via `markdownify` nach Markdown konvertiert. Formatting (fett, kursiv, Links, Listen) wird automatisch übernommen.

Heading-Tags (`h1`–`h6`) innerhalb der Container, deren Text dem Artikel-Titel entspricht, werden vor der Konvertierung entfernt (kein doppelter Titel).

### Skip-Logik: granular pro Datei

- `.md` existiert → Markdown-Erzeugung überspringen
- `.jpg` existiert → Bild-Download überspringen
- Beide existieren → Artikel komplett überspringen (kein HTTP-Request)

### Footer-Entfernung: `strip_signature()`

Sucht alle Zeilen, die fuzzy `"Joe Turan"` matchen (difflib-Ratio ≥ 0,75). Anchor-Strategie: Das **erste** "Joe Turan", dem innerhalb der nächsten 3 Zeilen `joeturan.com` folgt, markiert den Footer-Beginn. Alles ab dort wird abgeschnitten, ein sauberes `Joe Turan` wird angehängt. Fallback: letzte Übereinstimmung.

Behandelt alle bekannten Footer-Varianten:
- `Joe Turan` + `www.joeturan.com`
- `Joe Turan` + Social-Links + zweites `Joe Turan`

### CTA-Entfernung: `strip_cta_block()`

Arbeitet auf Paragraphen-Ebene. Jeder Absatz, der einen der Trigger-Ausdrücke enthält, wird entfernt. Unterstützt AND-Logik (Tuple = alle Phrasen müssen vorkommen).

Aktuelle Trigger (`_CTA_TRIGGERS`):

```python
("schreib mir", "whatsapp"),       # AND: beide müssen vorkommen
"erstgespräch",
"mit menschen arbeite",
"arbeite ich mit menschen",
"an dieser stelle arbeite ich",
"daran arbeite ich",
"kommst allein nicht",
```

Reihenfolge der Bereinigung in `extract_content_markdown()`:
1. `strip_cta_block()` — CTA-Absätze entfernen
2. `strip_signature()` — Footer normalisieren

### Logging

Dual-Handler: Konsole + `blogdownload.log` (UTF-8, append) im Script-Verzeichnis. Format: `yyyy-mm-dd HH:MM:SS  LEVEL  Meldung`. Fehler → `ERROR`, Bildwarnungen → `WARNING`, alles andere → `INFO`.

### Unveränderte Crawl-Logik

`visible_article_urls`, `click_next_page`, `process_listing_page`, `run` — Playwright-basiertes Crawling mit `data-page-next`-Pagination, max. 12 Artikel pro Seite. Nicht angefasst.

---

## Dependencies

```
beautifulsoup4
markdownify
Pillow
playwright
requests
```

Entfernt: `fpdf2`, `pypdf`

---

## Bekannte Schwachstellen / offene Punkte

- **CTA-Trigger können noch lückenhaft sein** — neue Varianten der Werbeabsätze werden laufend gemeldet und ergänzt. Muster: Absätze, die mit "Wenn du…" beginnen und auf Coaching-Angebot hinweisen.
- **`"daran arbeite ich"`** ist ein relativ kurzer Trigger — könnte theoretisch regulären Text treffen, falls der Autor diese Phrase im Artikel selbst verwendet. Noch kein Falsch-Positiv beobachtet.
- **Keine Fehler-Retry-Logik** bei Netzwerkfehlern (Bild-Download, Seiten-Abruf). Fehlgeschlagene Artikel werden geloggt und übersprungen.
- **Log rotiert nicht** — `blogdownload.log` wächst unbegrenzt an.
