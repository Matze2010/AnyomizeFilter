# AnymizeFilter

Ein [Open WebUI](https://github.com/open-webui/open-webui)-Filter, der personenbezogene Daten (PII) aus Chat-Eingaben entfernt, bevor sie an ein LLM gehen — und sie in der Antwort wieder einsetzt.

Die Anonymisierung selbst passiert nicht lokal, sondern über die externe API von [anymize.ai](https://app.anymize.ai). Der Filter ersetzt PII durch Platzhalter der Form `[[Typ-HASH]]` (z. B. `[[Person-QSEZB6]]`), schickt den maskierten Text ans Modell und macht die Maskierung nach der Antwort wieder rückgängig.

Der gesamte Filter steckt in einer Datei: [`anymize.py`](anymize.py).

---

## Funktionsweise

```mermaid
sequenceDiagram
    participant U as Nutzer
    participant OWUI as Open WebUI
    participant F as AnymizeFilter
    participant A as anymize.ai API
    participant LLM as LLM

    U->>OWUI: Nachricht (+ optional Dateien)
    OWUI->>F: inlet(body)
    opt Datei-Modi
        F->>A: POST /api/ocr (multipart, je Datei parallel)
        A-->>F: job_id
        F->>A: GET /api/status/{job_id} (Polling)
        A-->>F: anonymized_text_raw + systemprompt
    end
    opt Text-Modi
        F->>A: POST /api/anonymize (Text)
        A-->>F: job_id
        F->>A: GET /api/status/{job_id} (Polling)
        A-->>F: anonymized_text_raw + systemprompt
    end
    F-->>OWUI: letzte User-Message ersetzt durch maskierten Text
    OWUI->>LLM: Prompt mit [[Typ-HASH]]-Platzhaltern
    LLM-->>OWUI: Antwort mit Platzhaltern
    OWUI->>F: outlet(body)
    opt output_filter = deanonymized
        F->>A: POST /api/deanonymize
        A-->>F: Originaltext
    end
    F-->>U: Antwort mit echten Werten
```

### Ablauf im Detail

1. **`inlet()`** wird von Open WebUI vor dem LLM-Aufruf gerufen und delegiert an `process_input()`.
2. **Dateien** (Modi `file_anonymization`, `text_file_anonymization`): `get_file_paths()` baut aus `body["files"]` die Pfade `UPLOAD_DIR/{file_id}_{filename}`. `process_multiple_files_for_ocr()` lädt alle Dateien parallel an `POST /api/ocr` hoch — zusammen mit der in der Valve `language` gesetzten Sprache —, sammelt die Job-IDs und pollt sie parallel bis `status == "completed"`. Ergebnis: `anonymized_text_raw` je Datei.
3. **Text**: der Text der letzten User-Message wird an den OCR-Text angehängt.
4. **Anonymisierung** (Modi `text_anonymization`, `text_file_anonymization`): der kombinierte Inhalt geht mit der konfigurierten Sprache an `POST /api/anonymize`, die zurückgegebene `job_id` wird über `GET /api/status/{job_id}` gepollt. Der von der API gelieferte `systemprompt` (Regeln zum korrekten Umgang mit Platzhaltern) wird an den Inhalt angehängt. Anschließend holt `_store_hash_pairs()` über `GET /api/status/{job_id}/strings` die vollständige Zuordnungstabelle, legt sie für spätere Nachbearbeitung in `__metadata__` ab und schreibt sie per `logging.warning` ins Serverlog (siehe [Datenschutz-Hinweis](#datenschutz-hinweis)).
5. **Gegenprobe**: `_anonymize_locally()` wendet die Zuordnungstabelle lokal auf den Originaltext an — jedes `original` wird durch das zugehörige `placeholder` ersetzt — und `_compare_local_anonymization()` vergleicht das Ergebnis mit `anonymized_text_raw` der API. Bei Ungleichheit geht eine `logging.warning` ins Serverlog. Rein diagnostisch: ans LLM geht in jedem Fall der API-Text.
6. **Ersetzen**: die letzte User-Message im `body` wird durch den maskierten Inhalt überschrieben. Open WebUI schickt sie so ans Modell — das LLM sieht nur Platzhalter.
7. **`outlet()`** wird nach der LLM-Antwort gerufen. Bei `output_filter = "deanonymized"` geht die Assistant-Nachricht an `POST /api/deanonymize`; das Ergebnis ersetzt die Antwort im Chat. Bei `"anonymized"` bleibt sie unverändert, die Platzhalter bleiben sichtbar.
8. **Statusmeldungen** („Processing files…", „Anonymizing content…", „De-anonymizing content…") laufen über `__event_emitter__` und erscheinen im Chat-Verlauf.
9. **Fehler**: `inlet()` meldet `❌ Anonymization failed: …` und wirft eine Exception — die Anfrage geht nicht ans LLM. `outlet()` wirft nicht, sondern stellt der Originalantwort die Fehlermeldung voran, damit die Antwort nicht verloren geht.

---

## Installation

1. In Open WebUI: **Admin Panel → Functions → `+`** (neue Function anlegen).
2. Inhalt von [`anymize.py`](anymize.py) einfügen und speichern. Titel, Autor und Version zieht Open WebUI aus dem Docstring am Dateianfang.
3. Function aktivieren und entweder global oder pro Modell zuweisen.
4. Unter **Valves** den API-Key hinterlegen (siehe unten).
5. Im Chat lässt sich der Filter über sein Icon ein- und ausschalten (`self.toggle = True`). Ist er aus, geben `inlet()` und `outlet()` den `body` unverändert zurück.

---

## Konfiguration (Valves)

| Valve | Default | Bedeutung |
|---|---|---|
| `anymize_api_key` | `""` | API-Key von anymize.ai, Format `anymize_xxxxxxxxxxxxx`. Wird als `Authorization: Bearer …` gesendet. |
| `language` | `de` | Sprache der PII-Erkennung; gilt für `/api/anonymize` **und** `/api/ocr`. Mögliche Werte: `de`, `en`, `fr`, `es`, `it`. |
| `input_filter` | `text_anonymization` | Was vor dem LLM verarbeitet wird — siehe Modi unten. |
| `output_filter` | `deanonymized` | Was mit der LLM-Antwort passiert — siehe unten. |
| `priority` | `10` | Ausführungsreihenfolge unter mehreren Filtern; kleinere Werte laufen zuerst. |

### `input_filter`-Modi

| Wert | Verhalten |
|---|---|
| `text_anonymization` | Nur die letzte User-Message wird über `/api/anonymize` maskiert. Dateien werden ignoriert. |
| `file_anonymization` | Angehängte Dateien laufen durch `/api/ocr` (OCR + Anonymisierung in einem Schritt). Maskiert wird gezielt nur der Dateiinhalt; der getippte Text der User-Message wird unverändert angehängt. Wer beides maskieren will, nimmt `text_file_anonymization`. |
| `text_file_anonymization` | Dateien per OCR **und** der kombinierte Gesamttext anschließend per `/api/anonymize`. |

### `output_filter`-Modi

| Wert | Verhalten |
|---|---|
| `deanonymized` | Antwort geht an `/api/deanonymize`, echte Werte werden wieder eingesetzt. |
| `anonymized` | Antwort bleibt unverändert; die Platzhalter `[[Typ-HASH]]` bleiben im Chat stehen. |

---

## Genutzte API-Endpunkte

Basis-URL: `https://app.anymize.ai`, Auth über `Authorization: Bearer <api_key>`.

| Endpoint | Zweck | Aufruf im Code |
|---|---|---|
| `POST /api/anonymize` | Text maskieren, liefert `job_id` | [`_anonymize_text()`](anymize.py:139) |
| `GET /api/status/{job_id}` | Job-Status + `anonymized_text_raw` + `systemprompt` | [`_get_anonymization_status()`](anymize.py:148) |
| `GET /api/status/{job_id}/strings` | Zuordnungstabelle Platzhalter ↔ Originalwert; wird in `__metadata__` abgelegt und ins Serverlog geschrieben | [`_get_hash_pairs()`](anymize.py:152), [`_store_hash_pairs()`](anymize.py:156) |
| `POST /api/deanonymize` | Platzhalter zurück in Originalwerte | [`_deanonymize_text()`](anymize.py:237) |
| `POST /api/ocr` | Datei (PDF, PNG, JPG, TIFF) per multipart, OCR + Anonymisierung | [`upload_file_from_path_for_ocr()`](anymize.py:246) |

Details zu Parametern und Antwortformaten: [`anymize_api.md`](anymize_api.md) bzw. <https://app.anymize.ai/api-docs/anonymization>.

Nicht genutzt vom Filter: der Endpoint `/api/v1/llm-anonymous/chat/completions` (anonymer Chat in einem Schritt).

---

## Code-Aufbau

Alles in `class Filter` in [`anymize.py`](anymize.py):

| Gruppe | Methoden |
|---|---|
| Konfiguration | `Valves` (pydantic-Model), `__init__()` — setzt `toggle` und das Inline-SVG-Icon |
| Zuordnungstabelle | `_get_hash_pairs()`, `_store_hash_pairs()` — Abruf, Ablage in `__metadata__`, Logging |
| Gegenprobe | `_anonymize_locally()`, `_compare_local_anonymization()` — lokale Anonymisierung anhand der Tabelle, Vergleich mit dem API-Ergebnis |
| HTTP | `_anymize_api_request()` — aiohttp-Wrapper für GET/POST mit Bearer-Auth |
| API-Calls | `_anonymize_text()`, `_get_anonymization_status()`, `_deanonymize_text()`, `upload_file_from_path_for_ocr()` |
| Job-Polling | `_poll_status()` — pollt bis `status == "completed"`, `max_retries=150`, `retry_interval=10000` ms |
| Datei-Handling | `get_file_paths()`, `process_multiple_files_for_ocr()` — Upload und Polling parallel via `asyncio.gather()` |
| Orchestrierung | `process_input()` — sammelt Inhalte, ruft die Anonymisierung, überschreibt die letzte User-Message |
| Open-WebUI-Hooks | `inlet()` (vor dem LLM), `outlet()` (nach dem LLM) — beide mit `__metadata__` |

### Hash-Paare in `__metadata__`

`__metadata__` ist ein Live-Dict, das Open WebUI durch den Request-Lebenszyklus reicht: was `inlet()` hineinschreibt, sieht `outlet()` desselben Requests ([Doku](https://docs.openwebui.com/features/extensibility/plugin/functions/filter/#data-passing-between-filters)). `_store_hash_pairs()` legt die vollständige Zuordnungstabelle dort für die spätere Nachbearbeitung ab — pro Request, nicht als geteilter Zustand der Filter-Instanz:

| Key (`Filter.METADATA_*`) | Inhalt |
|---|---|
| `_anymize_hash_pairs` | `List[Dict[str, Any]]` — ein Dict je erkannter Entität |
| `_anymize_job_id` | `str` — Job-ID der zugehörigen Anonymisierung |

Zugriff in `outlet()`:

```python
hash_pairs = (__metadata__ or {}).get(Filter.METADATA_HASH_PAIRS_KEY, [])
```

Je Eintrag werden die in `Filter.HASH_PAIR_FIELDS` gelisteten Felder übernommen: `original`, `hash`, `prefix_name`, `internal_id`, `placeholder`. Fehlende Felder werden als `None` abgelegt — `internal_id` ist in [`anymize_api.md`](anymize_api.md) nicht dokumentiert und kann je nach API-Version fehlen.

Beide Hooks deklarieren `__metadata__` mit Default `None`, und `_store_hash_pairs()` schreibt nur, wenn das Dict tatsächlich übergeben wurde. Ohne Metadata läuft die Anonymisierung unverändert weiter, die Paare stehen dann nur im Log.

Eine reale Antwort von `GET /api/status/{job_id}/strings` sieht so aus:

```json
{
  "job_id": "dcd158d9-c654-453b-872a-30180abbc48c",
  "count": 1,
  "hash_pairs": [
    {
      "original": "DS.1.3-2026-1899",
      "hash": "S28MBV",
      "prefix_name": "internal_id",
      "placeholder": "[[internal_id-S28MBV]]"
    }
  ]
}
```

`placeholder` trägt das vollständige Token in der Form, in der es im maskierten Text steht, `hash` nur den nackten Code. Die Beispiele in [`anymize_api.md`](anymize_api.md) (`placeholder: "PERSON-1"`, `hash: "[PERSON-1]"`) geben das nicht wieder — sie sind veraltet.

### Lokale Gegenprobe

`_anonymize_locally()` wendet die Zuordnungstabelle selbst auf den Originaltext an: jedes `original` wird durch das Feld aus `Filter.HASH_PAIR_REPLACEMENT_FIELD` (`placeholder`) ersetzt. Paare, bei denen eines der beiden Felder fehlt, werden übersprungen. Ersetzt wird **absteigend nach Länge des Originalwerts** — sonst würde ein kurzer Wert, der Teilstring eines längeren ist (`Berlin` in `Berliner Str. 42, 10115 Berlin`), den längeren zerschneiden, bevor dieser an die Reihe kommt.

`_compare_local_anonymization()` vergleicht das Ergebnis mit `anonymized_text_raw` der API — verglichen wird vor dem Anhängen des `systemprompt`, den der lokale Text nicht enthält. Sind beide gleich, passiert nichts. Sonst geht eine `logging.warning` mit Job-ID, Länge beider Texte, Index der ersten Abweichung und je einem Kontextausschnitt (`Filter.DIFF_CONTEXT_CHARS` = 20 Zeichen je Seite) ins Log:

```
Anymize.ai local anonymization for job <id> differs from the API result:
local 34 chars, API 37 chars, first difference at index 28
  local: 'person-AAA111]] aus Berlin'
  api:   'person-AAA111]] aus [[loc-X]]'
```

Eine Abweichung heißt: der maskierte Text der API lässt sich mit der gelieferten Tabelle nicht reproduzieren — dann greift auch jede Nachbearbeitung, die auf der Tabelle aufsetzt, ins Leere. Der Vergleich ist rein diagnostisch: ans LLM geht in jedem Fall der API-Text, und Fehler in der Gegenprobe werden abgefangen und geloggt, statt den Request scheitern zu lassen. Ohne Hash-Paare (leere Liste, ZDR aktiv, Abruf fehlgeschlagen) entfällt der Vergleich.

---

## Voraussetzungen

- **Open WebUI** — der Filter importiert `open_webui.utils.misc.get_last_user_message` / `get_last_assistant_message` und `open_webui.config.UPLOAD_DIR`. Außerhalb von Open WebUI läuft die Datei nicht.
- **Python-Pakete**: `aiohttp`, `pydantic` (beide in Open WebUI bereits vorhanden).
- **anymize.ai-Account** mit API-Key.
- Für die De-Anonymisierung gilt laut API-Doku: der ursprüngliche Anonymisierungs-Job muss noch existieren, **Zero Data Retention (ZDR) muss im Account deaktiviert sein**, und nur derselbe Nutzer, der den Job erzeugt hat, darf de-anonymisieren. Mit aktivem ZDR liefert `/api/deanonymize` keine Originalwerte — dann ist nur `output_filter = "anonymized"` sinnvoll.

---

## Bekannte Einschränkungen

Stand der aktuellen Fassung von `anymize.py` (Version 1.0.0):

- **Langes blockierendes Polling**: `_poll_status()` versucht es bis zu 150-mal im Abstand von 10 s ([anymize.py:123](anymize.py:123)) — im Extremfall hängt eine Anfrage 25 Minuten, bevor der Timeout greift.
- **Kein Streaming-Support**: `outlet()` arbeitet auf der fertigen Nachricht. Bei aktivem Streaming sieht der Nutzer während der Ausgabe die rohen Platzhalter; erst am Ende wird ersetzt.
- **Nur die letzte User-Message wird anonymisiert**: Ältere Nachrichten des Verlaufs gehen unverändert ans LLM. In laufenden Unterhaltungen können frühere Klartext-PII also weiterhin mitgeschickt werden.
- **Job-ID im Log**: `logging.warning(f"Anymize.ai JobID: …")` ([anymize.py:416](anymize.py:416)) schreibt die Job-ID jeder Anonymisierung auf Warn-Level ins Serverlog.
- **`__metadata__` ist nicht in jedem Aufrufpfad garantiert**: Laut Open-WebUI-Doku läuft `outlet()` bei WebUI-Requests und über `/api/chat/completed`; für direkte Aufrufe von `/api/chat/completions` braucht es `ENABLE_API_OUTLET_FILTERS` auf `dev`/kommenden Releases. In Pfaden ohne Metadata stehen die Hash-Paare nur im Log, nicht im Dict.
- **Grenzen der Gegenprobe**: sie prüft nur, ob sich der maskierte Text der API aus der gelieferten Zuordnungstabelle reproduzieren lässt — **nicht**, ob die API PII übersehen hat. Übersehene PII steht in keiner der beiden Fassungen und fällt damit auch nicht auf.
- **Ungenutzte Importe**: `re` und `requests` werden importiert, aber nirgends verwendet.
- **`output_filter = "anonymized"`** lässt die Platzhalter dauerhaft in der Anzeige stehen — das ist so gewollt, überrascht aber, wenn der Wert versehentlich gesetzt ist.

---

## Datenschutz-Hinweis

Der Filter schickt Chat-Inhalte und hochgeladene Dateien an `app.anymize.ai` — die Daten verlassen also die eigene Infrastruktur, bevor sie maskiert sind. Das ist bauartbedingt: die Erkennung der PII passiert bei anymize.ai, nicht lokal. Ob das zulässig ist, hängt vom Einsatzzweck und der vertraglichen Grundlage (Auftragsverarbeitung) ab.

**Die Zuordnungstabelle landet im Log**: Nach jeder Text-Anonymisierung ruft `_store_hash_pairs()` die Hash-Paare ab und schreibt sie per `logging.warning` ins Open-WebUI-Serverlog — inklusive der Originalwerte im Klartext (`original='Max Mustermann'`). Die PII steht damit unmaskiert an einer Stelle, die der Filter sonst gerade schützt; wer Logs weiterleitet, aggregiert oder langfristig aufbewahrt, sollte das einkalkulieren. Abschalten geht nur durch Entfernen des Aufrufs in `process_input()`.

**Auch die Gegenprobe kann Klartext ins Log schreiben**: Weicht der lokal anonymisierte Text vom API-Ergebnis ab, enthält die Warnung je einen 40-Zeichen-Ausschnitt beider Fassungen. Der lokale Ausschnitt kann PII tragen, die die Tabelle nicht abdeckt — genau der Fall, den die Warnung meldet. Wer das nicht will, setzt `Filter.DIFF_CONTEXT_CHARS = 0` oder entfernt den Aufruf in `process_input()`.

Die zweite Kopie in `__metadata__` ist dagegen auf den einzelnen Request begrenzt und wird mit ihm verworfen — kein geteilter Zustand zwischen Nutzern oder Chats.

Zero Data Retention ist eine Kontoeinstellung bei anymize.ai, kein Request-Parameter. Mit ZDR speichert anymize.ai keine Zuordnung zwischen Platzhalter und Originalwert — dann funktioniert die De-Anonymisierung in `outlet()` nicht mehr.

---

## Autor & Lizenz

- Filter-Code: `bbojan` — <https://github.com/Bojan227>, Version 1.0.0 (aus dem Docstring in `anymize.py`).
- Dieses Repository: <https://github.com/Matze2010/AnymizeFilter>.
- Lizenz: nicht festgelegt — es liegt keine Lizenzdatei bei.
