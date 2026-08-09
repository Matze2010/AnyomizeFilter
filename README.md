# AnonymizeFilter

Ein [Open WebUI](https://github.com/open-webui/open-webui)-Filter, der personenbezogene Daten (PII) aus Chat-Eingaben entfernt, bevor sie an ein LLM gehen — und sie in der Antwort wieder einsetzt.

Die Anonymisierung selbst passiert nicht lokal, sondern über die externe API. Der Filter ersetzt PII durch Platzhalter der Form `[[Typ-HASH]]` (z. B. `[[Person-QSEZB6]]`), schickt den maskierten Text ans Modell und macht die Maskierung nach der Antwort wieder rückgängig.

Der gesamte Filter steckt in einer Datei: [`anonymize.py`](anonymize.py).

---

## Funktionsweise

```mermaid
sequenceDiagram
    participant U as Nutzer
    participant OWUI as Open WebUI
    participant F as AnymizeFilter
    participant A as Backend
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
    opt Tool-Call
        LLM-->>OWUI: tool_call
        OWUI->>OWUI: process_tool_result() (gepatcht)
        OWUI->>F: tool(tool_result)
        F->>A: POST /api/anonymize + Polling
        A-->>F: anonymized_text_raw
        F-->>OWUI: maskiertes Tool-Ergebnis
        OWUI->>LLM: erneuter Aufruf mit role="tool"
    end
    LLM-->>OWUI: Antwort mit Platzhaltern
    opt Streaming
        loop je Chunk
            OWUI->>F: stream(event)
            F-->>OWUI: event (Tool-Call-Argumente de-anonymisiert)
        end
    end
    OWUI->>F: outlet(body)
    opt output_filter = deanonymized
        F->>F: Platzhalter aus den Hash-Paaren ersetzen
    end
    F-->>U: Antwort mit echten Werten
```

### Ablauf im Detail

1. **`inlet()`** wird von Open WebUI vor dem LLM-Aufruf gerufen und delegiert an `process_input()`.
2. **Dateien** (Modi `file_anonymization`, `text_file_anonymization`): `get_file_paths()` baut aus `body["files"]` die Pfade `UPLOAD_DIR/{file_id}_{filename}`. `process_multiple_files_for_ocr()` lädt alle Dateien parallel an `POST /api/ocr` hoch — zusammen mit der in der Valve `language` gesetzten Sprache —, sammelt die Job-IDs und pollt sie parallel bis `status == "completed"`. Ergebnis: `anonymized_text_raw` je Datei.
3. **Text**: der Text der letzten User-Message wird an den OCR-Text angehängt.
4. **Anonymisierung** (Modi `text_anonymization`, `text_file_anonymization`): der kombinierte Inhalt geht mit der konfigurierten Sprache an `POST /api/anonymize`, die zurückgegebene `job_id` wird über `GET /api/status/{job_id}` gepollt. Der von der API gelieferte `systemprompt` (Regeln zum korrekten Umgang mit Platzhaltern) wird an den Inhalt angehängt. Bei `output_filter = "deanonymized"` holt `_store_hash_pairs()` anschließend über `GET /api/status/{job_id}/strings` die vollständige Zuordnungstabelle und legt sie in `__metadata__` ab — von dort lebt die gesamte Rückabbildung. Bei `"anonymized"` unterbleibt der Abruf.
5. **Ersetzen**: `final_content` ist immer der `anonymized_text_raw` der API, ergänzt um den `systemprompt`. Damit wird die letzte User-Message im `body` überschrieben. Open WebUI schickt sie so ans Modell — das LLM sieht nur Platzhalter.
6. **`tool()`** wird für jedes Tool-Ergebnis gerufen — nicht von Open WebUI, sondern aus dem Monkey Patch von `middleware.process_tool_result()`, den der Filter beim Laden installiert. Das bereits zu Text normalisierte Ergebnis läuft durch dieselbe Anonymisierung wie die User-Message und geht maskiert als `role="tool"`-Message ans Modell. Steht der Tool-Name in der Valve `tool_filter_exclude`, wird das Ergebnis unverändert durchgereicht. Siehe [Tool-Ergebnisse](#tool-ergebnisse).
7. **`stream()`** wird bei aktivem Streaming für jeden Chunk der Antwort gerufen. Chunks des Antworttexts gehen unverändert durch. Bei `output_filter = "deanonymized"` werden in Chunks mit `delta.tool_calls` die Platzhalter in `function.arguments` durch die Originalwerte aus der Zuordnungstabelle in `__metadata__` ersetzt, bevor Open WebUI die Argumente zusammensetzt und das Tool aufruft — siehe [Tool-Call-Argumente](#tool-call-argumente).
8. **`outlet()`** wird nach der LLM-Antwort gerufen. Zuerst schreibt `_log_hash_pairs()` die in `__metadata__` gesammelte Zuordnungstabelle einmal ins Serverlog — ohne die Originalwerte. Danach: bei `output_filter = "deanonymized"` werden die Platzhalter der Assistant-Nachricht lokal aus denselben Paaren ersetzt, mit denen `stream()` arbeitet; kein Request ans Backend. Ersetzt wird in `message["content"]` **und** in den strukturierten `message["output"]`-Blöcken: das Frontend rendert eine Assistant-Nachricht seit Open WebUI 0.10 aus diesen Blöcken und greift auf `content` nur zurück, wenn keine da sind (`ContentRenderer.svelte`: `{#if output?.length}`) — eine gestreamte Antwort trägt sie immer, ohne diesen Schritt bliebe der Text mit Platzhaltern auf dem Bildschirm stehen. Jedes Textelement wird für sich ersetzt, Reihenfolge und Anzahl der Blöcke bleiben unangetastet; der Ansatz stammt aus dem PoC in [`hook_logger.py`](hook_logger.py). Bei `"anonymized"` bleibt die Antwort unverändert, die Platzhalter bleiben sichtbar.
9. **Statusmeldungen** („Processing files…", „Anonymizing content…", „Anonymizing result of &lt;tool&gt;…", „De-anonymizing content…") laufen über `__event_emitter__` und erscheinen im Chat-Verlauf.
10. **Fehler**: `inlet()` meldet `❌ Anonymization failed: …` und wirft eine Exception — die Anfrage geht nicht ans LLM. `tool()` wirft nicht, sondern ersetzt das Tool-Ergebnis durch die Fehlermeldung (fail closed). `outlet()` wirft ebenfalls nicht, sondern stellt der Originalantwort die Fehlermeldung voran, damit die Antwort nicht verloren geht.

---

## Installation

1. In Open WebUI: **Admin Panel → Functions → `+`** (neue Function anlegen).
2. Inhalt von [`anonymize.py`](anonymize.py) einfügen und speichern. Titel, Autor und Version zieht Open WebUI aus dem Docstring am Dateianfang.
3. Function aktivieren und entweder global oder pro Modell zuweisen.
4. Unter **Valves** den API-Key hinterlegen (siehe unten).
5. Im Chat lässt sich der Filter über sein Icon ein- und ausschalten (`self.toggle = True`). Ist er aus, ruft Open WebUI `inlet()`, `stream()` und `outlet()` gar nicht erst auf — `self.toggle` bleibt dabei unverändert `True`, den Zustand des Schalters trägt allein `metadata["filter_ids"]`. Weil Tool-Ergebnisse nicht über die Filter-Pipeline laufen, sondern über den Monkey-Patch, prüft `_filter_enabled()` diese Liste dort ein zweites Mal; ist der Schalter aus, geht das Tool-Ergebnis unverändert an das Modell. Lässt sich der Zustand nicht ermitteln (fehlende Metadaten, ältere Open-WebUI-Version), wird sicherheitshalber anonymisiert.

---

## Konfiguration (Valves)

| Valve | Default | Bedeutung |
|---|---|---|
| `backend_url` | z.B. `https://app.anymize.ai` | Basis-URL des anymize-Backends, ohne Pfad. Nur ändern, um eine self-hosted oder Staging-Instanz anzusprechen. Ein abschließender `/` wird abgeschnitten; leer gelassen greift wieder der Default. |
| `anymize_api_key` | `""` | API-Key des Backends, Format z.B. `anymize_xxxxxxxxxxxxx`. Wird als `Authorization: Bearer …` gesendet. |
| `language` | `de` | Sprache der PII-Erkennung; gilt für `/api/anonymize` **und** `/api/ocr`. Mögliche Werte: `de`, `en`, `fr`, `es`, `it`. |
| `label_ids` | `""` | Kommagetrennte Liste von Label-IDs, die als `label_ids` bei jedem `POST /api/anonymize` mitgeschickt wird und die Erkennung auf diese Entitätstypen einschränkt, z.B. `PERSON, IBAN`. Die Werte gehen unverändert raus, Groß-/Kleinschreibung zählt. Gilt für User-Input **und** Tool-Ergebnisse, nicht für `/api/ocr`. Leer = das Feld wird gar nicht gesendet, es greift der Default des Backends. |
| `input_filter` | `text_anonymization` | Was vor dem LLM verarbeitet wird — siehe Modi unten. |
| `output_filter` | `deanonymized` | Was mit der LLM-Antwort passiert — siehe unten. |
| `tool_filter` | `true` | Maskiert das Ergebnis jedes Tool-Calls, bevor es das Modell sieht — siehe [Tool-Ergebnisse](#tool-ergebnisse). Braucht den Monkey Patch von `middleware.process_tool_result`, der beim Laden des Filters installiert wird. |
| `tool_filter_exclude` | `""` | Kommagetrennte Liste von Tool-Namen, deren Ergebnis **nicht** maskiert und unverändert an das LLM durchgereicht wird, z.B. `get_time, enaio_*`. Groß-/Kleinschreibung egal, Wildcards erlaubt — siehe [Ausgeschlossene Tools](#ausgeschlossene-tools). Wirkt nur, solange `tool_filter` an ist. ⚠️ Das Ergebnis eines ausgeschlossenen Tools erreicht das Modell im Klartext. Leer = jedes Tool-Ergebnis wird maskiert. |
| `log_tool_payload` | `false` | Schreibt die Parameter jedes `process_tool_result()`-Aufrufs sowie jedes Tool-Ergebnis vor und nach der Maskierung vollständig ins Serverlog. ⚠️ Das rohe Ergebnis steht damit im Klartext im Log. Aus = kompakte Zeilen mit Typ, Länge und Korrelations-IDs. |
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
| `deanonymized` | Der einzige Schalter für die gesamte Rückabbildung: nach jedem Job wird die Zuordnungstabelle über `GET /api/status/{job_id}/strings` geholt und in `__metadata__` gesammelt, `stream()` setzt die Originalwerte in die Argumente eines Tool-Calls ein (siehe [Tool-Call-Argumente](#tool-call-argumente)), `outlet()` in die Antwort. Die Ersetzung läuft lokal aus den Paaren, ohne weiteren Backend-Request. |
| `anonymized` | Es wird keine Zuordnungstabelle abgerufen. Die Platzhalter `[[Typ-HASH]]` bleiben überall stehen — im Chat **und** in den Argumenten eines Tool-Calls, der damit sein Ziel nicht findet. |

---

## Genutzte API-Endpunkte

Basis-URL: die Valve `backend_url`, per Default `https://app.anymize.ai`. Auth über `Authorization: Bearer <api_key>`. Alle Endpunkte unten hängen an dieser Basis, auch der Multipart-Upload nach `/api/ocr`.

| Endpoint | Zweck | Aufruf im Code |
|---|---|---|
| `POST /api/anonymize` | Text maskieren, liefert `job_id`; Body `text` + `language`, dazu optional `label_ids` aus der gleichnamigen Valve | [`_anonymize_text()`](anonymize.py:431) |
| `GET /api/status/{job_id}` | Job-Status + `anonymized_text_raw` + `systemprompt` | [`_get_anonymization_status()`](anonymize.py:440) |
| `GET /api/status/{job_id}/strings` | Zuordnungstabelle Platzhalter ↔ Originalwert; nur bei `output_filter = "deanonymized"`, wird dann in `__metadata__` abgelegt und ist die Grundlage jeder Rückabbildung | [`_get_hash_pairs()`](anonymize.py:687), [`_store_hash_pairs()`](anonymize.py:699), [`_log_hash_pairs()`](anonymize.py:740) |
| `POST /api/ocr` | Datei (PDF, PNG, JPG, TIFF) per multipart, OCR + Anonymisierung | [`upload_file_from_path_for_ocr()`](anonymize.py:527) |


---

## Code-Aufbau

Alles in `class Filter` in [`anonymize.py`](anonymize.py):

| Gruppe | Methoden |
|---|---|
| Konfiguration | `Valves` (pydantic-Model), `__init__()` — setzt `toggle` und das Inline-SVG-Icon, `base_url` (Property) — Basis-URL aus der Valve `backend_url`, ohne abschließenden `/` |
| Zuordnungstabelle | `_get_hash_pairs()`, `_store_hash_pairs()` — Abruf und Ablage in `__metadata__`, nur bei `output_filter = "deanonymized"`; `_log_hash_pairs()` — Ausgabe der gesammelten Tabelle ohne Originalwerte (`HASH_PAIR_LOG_FIELDS`), einmal je Request aus `outlet()`; `_deanonymize` (Property) — der eine Modus-Check, den `_store_hash_pairs()`, `stream()` und `outlet()` teilen |
| HTTP | `_anymize_api_request()` — aiohttp-Wrapper für GET/POST mit Bearer-Auth |
| API-Calls | `_anonymize_text()`, `_get_anonymization_status()`, `upload_file_from_path_for_ocr()` |
| Job-Polling | `_poll_status()` — pollt bis `status == "completed"`, `max_retries=150`, `retry_interval=10000` ms |
| Datei-Handling | `get_file_paths()`, `process_multiple_files_for_ocr()` — Upload und Polling parallel via `asyncio.gather()` |
| Orchestrierung | `_anonymize_content()` — ein Text von der Abgabe bis zum maskierten Ergebnis (Job, Polling, Zuordnungstabelle); genutzt von `process_input()` und `tool()`. `process_input()` — sammelt Inhalte, ruft die Anonymisierung, überschreibt die letzte User-Message |
| Open-WebUI-Hooks | `inlet()` (vor dem LLM), `stream()` (je Chunk), `outlet()` (nach dem LLM) — alle mit `__metadata__` |
| Rückabbildung | Modul-Ebene: `_placeholder_map()` — Platzhalter → Originalwert aus den Hash-Paaren, mit `for_json=True` JSON-escaped für `stream()`, roh für `outlet()`; `_replace_placeholders()` — die Ersetzung selbst, von beiden genutzt; `_split_pending()` — hält einen über Chunk-Grenzen zerteilten Platzhalter zurück. In der Klasse: `_deanonymize_tool_calls()` — bearbeitet einen Chunk; `_deanonymize_message()` — ersetzt in einer Assistant-Message, `_output_text_items()` — findet den vom Frontend gerenderten Text in `message["output"]`, `_prefix_assistant_text()` — stellt der Antwort die Fehlermeldung voran; `_log_missing_pairs()` — Warnung einmal je Request |
| Tool-Ergebnisse | Modul-Ebene: `_unwrap()`, `_bind_tool_call()`, `_anonymize_tool_result()`, `_install_process_tool_result_patch()` — Monkey Patch; `_filter_enabled()` — Zustand des Chat-Schalters aus `metadata["filter_ids"]`; `_parse_tool_patterns()`, `_tool_excluded()` — Auswertung der Valve `tool_filter_exclude`. In der Klasse: `tool()` — siehe [Tool-Ergebnisse](#tool-ergebnisse) |

### Hash-Paare in `__metadata__`

> Dieser Abschnitt greift nur bei `output_filter = "deanonymized"`. Im Modus `"anonymized"` wird die Tabelle nicht abgerufen, und in `__metadata__` steht nichts davon.

`__metadata__` ist ein Live-Dict, das Open WebUI durch den Request-Lebenszyklus reicht: was `inlet()` hineinschreibt, sieht `outlet()` desselben Requests ([Doku](https://docs.openwebui.com/features/extensibility/plugin/functions/filter/#data-passing-between-filters)). `_store_hash_pairs()` legt die vollständige Zuordnungstabelle dort ab — pro Request, nicht als geteilter Zustand der Filter-Instanz. `stream()` und `outlet()` lesen anschließend daraus:

| Key (`Filter.METADATA_*`) | Inhalt |
|---|---|
| `_anymize_hash_pairs` | `List[Dict[str, Any]]` — ein Dict je erkannter Entität, **angehängt** über alle Jobs des Requests (Eingabe und Tool-Ergebnisse) |
| `_anymize_job_id` | `str` — Job-ID der Eingabe-Anonymisierung aus `inlet()` |
| `_anymize_tool_job_ids` | `List[str]` — Job-IDs der in diesem Request anonymisierten Tool-Ergebnisse, in Aufrufreihenfolge |
| `_anymize_tool_call_buffer` | `Dict[int, str]` — je Tool-Call-Index das Ende, das `stream()` zurückhält, weil daraus noch ein Platzhalter werden kann |
| `_anymize_tool_call_no_pairs_logged` | `bool` — gesetzt, sobald `stream()` oder `outlet()` das Fehlen der Zuordnungstabelle gemeldet hat, damit die Warnung einmal je Request steht statt einmal je Chunk |

Zugriff in `outlet()`:

```python
hash_pairs = (__metadata__ or {}).get(Filter.METADATA_HASH_PAIRS_KEY, [])
```

Je Eintrag werden die in `Filter.HASH_PAIR_FIELDS` gelisteten Felder übernommen: `original`, `hash`, `prefix_name`, `placeholder`. Fehlende Felder werden als `None` abgelegt.

Alle drei Hooks deklarieren `__metadata__` mit Default `None`, und `_store_hash_pairs()` schreibt nur, wenn das Dict tatsächlich übergeben wurde **und** der Modus stimmt. Ohne Metadata läuft die Anonymisierung unverändert weiter, die Rückabbildung nicht: `stream()`, `outlet()` und `_log_hash_pairs()` lesen genau aus diesem Dict.

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

`placeholder` trägt das vollständige Token in der Form, in der es im maskierten Text steht, `hash` nur den nackten Code.

**Ohne Hash-Paare bleibt die Maskierung intakt, die Rückabbildung nicht.** Die Zuordnungstabelle kann leer sein, weil der Abruf fehlgeschlagen ist, weil die API keine PII erkannt hat oder weil Zero Data Retention aktiv ist — ZDR ist eine Kontoeinstellung, kein Request-Parameter, und von hier aus nur daran sichtbar, dass `GET /api/status/{job_id}/strings` nichts liefert. Der maskierte Text kommt in allen Fällen weiterhin von der API; nur die Rückabbildung in `stream()` und `outlet()` findet dann keine Originalwerte mehr — die Platzhalter bleiben in der Antwort und in den Tool-Argumenten stehen, mit einer Warnung je Request (`no hash pairs available for this request`). `_store_hash_pairs()` schreibt in dem Fall eine Warnung ohne PII ins Log (`none returned`), ebenso wenn der Abruf selbst fehlschlägt (`unavailable`).

### Tool-Ergebnisse

Filter-Hooks decken nur die Chat-Grenze ab. Das Ergebnis eines Tool-Calls passiert weder `inlet()` noch `stream()` noch `outlet()`: Open WebUI führt das Tool in `utils/middleware.py` aus, normalisiert das Ergebnis in `process_tool_result()`, hängt es als `role="tool"`-Message an und ruft das Modell erneut — ohne die Filter-Kette erneut zu betreten. Ein Tool, das eine Akte, eine Datenbank oder ein Postfach liest, würde damit trotz aktivem Filter PII im Klartext ans LLM geben.

Deshalb patcht `anonymize.py` beim Laden `middleware.process_tool_result`:

```python
async def process_tool_result(request, tool_function_name, tool_result, tool_type,
                              direct_tool=False, metadata=None, user=None)
    -> (tool_result, tool_result_files, tool_result_embeds)
```

Die Funktion ist in `middleware` modulglobal definiert und wird über den Modul-Globalnamen aufgerufen — `middleware.process_tool_result = patched` greift deshalb für alle Aufrufstellen (natives und Prompt-basiertes Function Calling, MCP, `direct=True`-Tools).

**Der Wrapper läuft nach dem Original.** Zu diesem Zeitpunkt sind `dict`, `list`, `tuple`, `HTMLResponse` und MCP-Items bereits zu einem String normalisiert; `tool()` sieht also immer nur Text. Der String geht durch dasselbe `_anonymize_content()` wie die User-Message. Der `systemprompt` der API wird dabei **nicht** angehängt — er gehört an die User-Message, wo `inlet()` ihn bereits gesetzt hat.

**Fehler laufen fail closed**: schlägt die Anonymisierung fehl (Backend nicht erreichbar, Timeout), ersetzt `tool()` das Ergebnis durch `❌ Anonymization of the result of <tool> failed: … The result was withheld.` Das rohe Ergebnis erreicht das Modell nie; der Chat läuft weiter, das Modell sieht einen fehlgeschlagenen Tool-Call. Dasselbe gilt, wenn der Wrapper selbst scheitert.

Die Platzhalter aus Tool-Ergebnissen löst `outlet()` mit auf: `_store_hash_pairs()` hängt die Paare jedes Jobs an dieselbe Tabelle in `__metadata__`, aus der die Ersetzung liest.

#### Chat-Schalter

Derselbe Umweg, der die Tool-Ergebnisse überhaupt erreichbar macht, schneidet sie auch vom Ein-/Aus-Schalter im Chat ab: Der Monkey Patch hängt an `middleware` und läuft für **jeden** Tool-Call der Instanz, unabhängig davon, welche Filter die Anfrage aktiviert hat. `self.toggle` hilft dabei nicht — Open WebUI liest das Attribut einmal beim Aufbau der Filter-Kette und schreibt es nie zurück, es bleibt also immer `True`. Der Zustand des Schalters steckt allein in der Liste `metadata["filter_ids"]`, die der Client mitschickt (`main.py`: `"filter_ids": form_data.pop("filter_ids", [])`) und die auch `process_tool_result()` erhält.

`_filter_enabled()` wertet sie in `tool()` als Erstes aus, vor Valves, Ausschlussliste und Event-Emitter, und wendet damit dieselbe Regel an, mit der Open WebUI über `inlet()`/`outlet()` entscheidet. Die eigene Function-ID liest das Modul aus seinem eigenen `__name__`, das Open WebUI beim Laden als `function_<id>` setzt. Ist der Schalter aus, geht das Tool-Ergebnis unverändert an das Modell — kein Job, keine Statusanzeige, eine Zeile im Log.

Die Prüfung **fällt sicherheitshalber auf „an" zurück**: Lässt sich die eigene ID nicht bestimmen, fehlen die Metadaten oder kennt die Open-WebUI-Version den Key `filter_ids` nicht, wird anonymisiert. Ein Tool-Ergebnis bei ausgeschaltetem Filter zu maskieren ist lästig, PII im Klartext ans Modell zu geben nicht.

#### Ausgeschlossene Tools

Nicht jedes Tool liefert PII. Ein Tool, das Aktenzeichen, IDs, Statuswerte, Zeitreihen oder Code zurückgibt, verliert durch die Maskierung Information, kostet einen eigenen Anonymisierungs-Job samt Polling und schickt seinen Text an die externe API. Die Valve `tool_filter_exclude` nimmt solche Tools heraus:

```
tool_filter_exclude = get_time, enaio_*, list_users
```

Verglichen wird gegen `tool_function_name`, also den Namen, unter dem das Tool bei Open WebUI registriert ist (dieselbe Zeichenkette, die im Aufruf-Log als `tool_function_name='…'` steht — siehe unten).

- **Exakter Vergleich**, kein Substring: `get_time` trifft `get_time`, aber nicht `get_time_zone`.
- **Groß-/Kleinschreibung egal**, führende und abschließende Leerzeichen werden abgeschnitten, leere Einträge ignoriert.
- **Shell-Wildcards** (`*`, `?`, `[…]`) über `fnmatch`: `enaio_*` schließt alle Tools eines MCP-Servers mit diesem Präfix aus, `*` schaltet die Tool-Anonymisierung faktisch ab.
- Ein **unbrauchbares Muster** führt zu keinem Treffer und einer Log-Warnung; das Ergebnis wird dann normal maskiert, nicht versehentlich durchgereicht.

Ein Treffer überspringt den kompletten Pfad: kein Job, kein Aufruf der externen API, keine Statusanzeige im Chat. Das Ergebnis geht byte-identisch an das Modell, protokolliert wird nur eine Zeile im Serverlog. Das Aufruf-Logging von `process_tool_result()` bleibt davon unberührt — der Call selbst erscheint weiterhin im Log.

Unterschied zu `tool_filter = false`: Letzteres reicht **jedes** Tool-Ergebnis unverändert durch; `tool_filter_exclude` wirkt nur auf die genannten Namen und wird gar nicht erst ausgewertet, wenn `tool_filter` aus ist.

Die Patch-Mechanik entspricht der von [`hook_logger.py`](hook_logger.py): `_PATCH_FLAG`/`_PATCH_ORIGINAL` markieren den eigenen Wrapper, `_unwrap()` schält ihn vor der Neuinstallation ab. Nötig ist das, weil Open WebUI das Function-Modul bei jeder Valve-Änderung neu ausführt — ohne Abschälen stapeln sich Wrapper, die die Valves einer alten Instanz lesen und je Tool-Call mehrere Anonymisierungs-Jobs auslösen würden. Fehlt `process_tool_result` (ältere Open-WebUI-Version), läuft der Filter ohne Tool-Anonymisierung weiter und schreibt eine Warnung ins Log.

**Statusanzeige**: `process_tool_result()` bekommt keinen Event-Emitter — der liegt im Aufrufer `tool_call_handler` als Closure-Variable. `tool()` baut sich deshalb über `get_event_emitter(__metadata__)` aus `open_webui.socket.main` einen eigenen, genau wie Open WebUI es für die Filter-Hooks tut; die Funktion liest `user_id`, `chat_id` und `message_id` aus dem Dict. Fehlt einer der Keys oder das Modul, entfällt die Anzeige stillschweigend, die Anonymisierung läuft weiter.

| Zeitpunkt | Statusmeldung |
|---|---|
| während des Jobs | „Anonymizing result of &lt;tool&gt;…" (sichtbar) |
| nach Erfolg | leer, `hidden` — die Meldung verschwindet |
| bei Fehlschlag | „❌ Anonymization of the result of &lt;tool&gt; failed: …", `done` — bleibt stehen |

**Aufruf-Logging**: Jeder Aufruf von `process_tool_result()` wird protokolliert, bevor das Original läuft — ein Aufruf, der danach hängt, ist damit trotzdem im Log sichtbar. Die Zeile listet alle Funktionsparameter mit Namen, gebildet aus den über `inspect.signature()` gebundenen Argumenten; kommen in einer künftigen Open-WebUI-Version Parameter dazu, erscheinen sie von selbst mit.

```
Anymize process_tool_result: request=<Request> tool_function_name='read_case' tool_result=<str len=1830> tool_type='mcp' direct_tool=False metadata={chat_id='…', message_id='…', session_id='…', user_id='…'} keys=17 user={id='…'} keys=9
```

Ohne `log_tool_payload` steht vom `tool_result` nur Typ und Länge in der Zeile — unabhängig davon, wie kurz es ist —, von `metadata` und `user` nur die Korrelations-IDs und die Anzahl der Keys, und von anderen Objekten Typ und Größe. Mit `log_tool_payload = true` steht jeder Parameter vollständig im Log, inklusive des rohen Tool-Ergebnisses.

Erwartete Logzeilen:

| Fall | Meldung |
|---|---|
| Patch installiert | `Anymize <version>: process_tool_result patch installed` |
| Modul neu geladen | `Anymize: replaced the process_tool_result patch of an older version` |
| Funktion fehlt | `Anymize: middleware.process_tool_result not found — tool results are NOT anonymized on this Open WebUI version` |
| Aufruf | `Anymize process_tool_result: <alle Parameter>` |
| Tool-Ergebnis maskiert | `Anonymize tool_result: name=<tool> len=<vorher> -> <nachher>` |
| Tool-Ergebnis übersprungen | `Anonymize tool_result skipped: name=<tool> len=<n> — excluded by tool_filter_exclude, handed to the model unchanged` |
| Fehlschlag | `Anonymize tool_result failed: name=<tool> … — result withheld from the model` |

---

### Tool-Call-Argumente

Das Modell sieht ausschließlich maskierten Text und setzt Platzhalter deshalb auch in die **Argumente** eines Tool-Calls:

```json
{"index": 0, "id": "call_xvu97e5i", "type": "function",
 "function": {"name": "enaio_list_running_cases",
              "arguments": "{\"SessionID\": \"B674…\", \"user\": \"[[Person-HGCVVW]]\"}"}}
```

Ohne Eingriff ruft Open WebUI das Tool mit `[[Person-HGCVVW]]` als Benutzerkürzel auf — der Call läuft, liefert aber nichts. `stream()` ist die letzte Stelle vor der Ausführung: Open WebUI sammelt die Fragmente von `delta.tool_calls[].function.arguments` je Index ein und startet das Tool erst danach.

Bei `output_filter = "deanonymized"` ersetzt der Filter dort jeden bekannten Platzhalter durch seinen Originalwert. Die Ersetzung ist rein lokal, es geht kein Request ans Backend; die Zuordnung kommt aus den Hash-Paaren in `__metadata__`, die derselbe Modus vorher geholt hat.

Drei Details:

- **JSON-Escaping**: `arguments` ist JSON-*Text*. Der Originalwert wird über `json.dumps(..., ensure_ascii=False)` escaped eingesetzt, sonst zerlegt ein Anführungszeichen oder ein Zeilenumbruch im Originalwert das JSON. Umlaute bleiben als Umlaute stehen.
- **Zerteilte Platzhalter**: Ein Platzhalter kann über zwei Chunks zerfallen (`…"user": "[[Per` / `son-HGCVVW]]"}`). Ein nicht geschlossenes `[[` am Chunk-Ende wird deshalb zurückgehalten und mit dem nächsten Chunk ausgegeben; der Puffer liegt in `__metadata__[_anymize_tool_call_buffer]`, nicht auf der Filter-Instanz, die sich alle Chats teilen. Spätestens beim Chunk mit `finish_reason` wird alles ausgegeben — notfalls als eigener `tool_calls`-Delta-Eintrag, den Open WebUI über den Index demselben Tool-Call zuordnet. Ein `[[`, das länger als 64 Zeichen offen bleibt, gilt als normaler Text und wird freigegeben.
- **Fail open**: Bricht hier etwas, geht der Chunk unverändert weiter. Ein Platzhalter im Tool-Aufruf ist ein fehlschlagender Tool-Call, kein Datenabfluss — eine werfende `stream()`-Funktion würde dagegen jede Antwort zerstören.

| Fall | Meldung |
|---|---|
| Ersetzt | `Anonymize tool_call: name=<tool> placeholders replaced with their original values: [[Person-…]]` |
| Keine Zuordnung | `Anonymize: no hash pairs available for this request — …` (einmal je Request, geteilt mit `outlet()`) |
| Fehlschlag | `Anonymize tool_call de-anonymization failed: … — chunk passed on unchanged` |

---

## Voraussetzungen

- **Open WebUI** — der Filter importiert `open_webui.utils.misc.get_last_user_message` / `get_last_assistant_message` und `open_webui.config.UPLOAD_DIR`. Außerhalb von Open WebUI läuft die Datei nicht.
- **Python-Pakete**: `aiohttp`, `pydantic` (beide in Open WebUI bereits vorhanden).
- API-Key eines Anonymisierungs-Dienstes.
- Für die Rückabbildung gilt: sie hängt vollständig an `GET /api/status/{job_id}/strings`, das direkt nach der Anonymisierung im selben Request abgefragt wird. **Zero Data Retention (ZDR) muss im Account deaktiviert sein** — mit aktivem ZDR speichert das Backend keine Zuordnung, der Abruf liefert nichts, und nur `output_filter = "anonymized"` ist sinnvoll. Dass der Job später abläuft, spielt dagegen keine Rolle mehr: die Paare liegen ab dem Abruf im Request.

---

## Bekannte Einschränkungen

Stand der aktuellen Fassung von `anonymize.py` (Version 1.3.0):

- **Jeder Eintrag in `tool_filter_exclude` ist ein bewusster Klartext-Pfad zum LLM**: Das Ergebnis eines ausgeschlossenen Tools wird nicht geprüft, sondern ungesehen durchgereicht — liefert das Tool später doch PII (geänderte Query, erweiterte API, anderer Datenbestand), fällt das nicht auf. Ein zu weit gefasstes Wildcard (`*` oder ein Präfix, das mehr Tools trifft als gedacht) schaltet die Tool-Anonymisierung faktisch ab, ohne dass `tool_filter` auf `false` steht.

- **Der Patch von `process_tool_result()` greift in Open-WebUI-Interna ein** und kann bei jedem Upgrade brechen. Ändert sich die Signatur, wird sie über `inspect.signature()` weiterhin korrekt gebunden; verschwindet die Funktion, entfällt die Tool-Anonymisierung stillschweigend bis auf eine Log-Warnung.
- **`tool_result_files` und `tool_result_embeds` bleiben unmaskiert**: Inline-HTML-Embeds und Bilder eines Tools reicht der Wrapper unverändert durch. Sie gehen an das Frontend, nicht an das LLM — im Chat sichtbare PII ist damit weiterhin möglich.
- **Jeder Tool-Call kostet einen eigenen Anonymisierungs-Job** inklusive Polling. Bei mehreren Tool-Calls je Antwort summiert sich das spürbar (siehe nächster Punkt).
- **Langes blockierendes Polling**: `_poll_status()` versucht es bis zu 150-mal im Abstand von 10 s ([anonymize.py:415](anonymize.py:415)) — im Extremfall hängt eine Anfrage 25 Minuten, bevor der Timeout greift.
- **Keine Streaming-De-Anonymisierung des Antworttexts**: `stream()` bearbeitet nur die Argumente von Tool-Calls; die Chunks des Antworttexts gehen unverändert durch. Die De-Anonymisierung der Antwort bleibt in `outlet()` auf der fertigen Nachricht, der Nutzer sieht während der Ausgabe weiterhin die rohen Platzhalter.
- **Im Modus `anonymized` laufen Tool-Calls ins Leere**: ohne Zuordnungstabelle bekommt ein Tool `[[Person-HGCVVW]]` als Parameter und findet nichts. Das ist die Konsequenz daraus, dass ein Schalter beides steuert, und im Log an einer Warnung je Request erkennbar.
- **Ein zusätzlicher GET je Anonymisierungs-Job**: `GET /api/status/{job_id}/strings` läuft im Modus `deanonymized` — dem Default — nach jedem Job, also auch nach jedem Tool-Ergebnis. Dafür entfällt der frühere `POST /api/deanonymize` je Antwort.
- **Nur die letzte User-Message wird anonymisiert**: Ältere Nachrichten des Verlaufs gehen unverändert ans LLM. In laufenden Unterhaltungen können frühere Klartext-PII also weiterhin mitgeschickt werden.
- **Job-ID im Log**: `logging.warning(f"Anymize JobID: …")` ([anonymize.py:640](anonymize.py:640)) schreibt die Job-ID jeder Anonymisierung auf Warn-Level ins Serverlog.
- **`__metadata__` ist nicht in jedem Aufrufpfad garantiert**: Laut Open-WebUI-Doku läuft `outlet()` bei WebUI-Requests und über `/api/chat/completed`; für direkte Aufrufe von `/api/chat/completions` braucht es `ENABLE_API_OUTLET_FILTERS` auf `dev`/kommenden Releases. In Pfaden ohne Metadata gibt es keine Rückabbildung: die Paare können nirgends abgelegt werden, und `stream()` wie `outlet()` finden nichts zum Ersetzen.
- **Die `output`-Blöcke sind an die Open-WebUI-Struktur gebunden**: `outlet()` schreibt die Antwort nach `content` **und** in die `message["output"]`-Blöcke, weil das Frontend seit 0.10 daraus rendert. Erwartet wird dabei `{"type": "message", "content": [{"type": "output_text", "text": …}]}`; ändert Open WebUI dieses Format, greift die Ersetzung nur noch in `content` — sichtbar bleibt dann wieder der Text mit Platzhaltern. Jede Ebene wird einzeln geprüft, ein unbekannter Aufbau führt nicht zu einem Fehler.

---

## Datenschutz-Hinweis

Der Filter schickt Chat-Inhalte und hochgeladene Dateien an externe APIs — die Daten verlassen also die eigene Infrastruktur, bevor sie maskiert sind. Das ist bauartbedingt: die Erkennung der PII passiert extern, nicht lokal. Ob das zulässig ist, hängt vom Einsatzzweck und der vertraglichen Grundlage (Auftragsverarbeitung) ab.

**Die Zuordnungstabelle liegt im Request, im Log stehen keine Originalwerte**: Bei `output_filter = "deanonymized"` ruft `_store_hash_pairs()` nach jeder Anonymisierung die Hash-Paare ab und sammelt sie in `__metadata__` — dort stehen Platzhalter **und** Originalwerte, denn genau daraus arbeitet die Rückabbildung. `_log_hash_pairs()` schreibt am Ende des Requests nur die Felder aus `HASH_PAIR_LOG_FIELDS` ins Serverlog (`placeholder`, `hash`, `prefix_name`); der Originalwert ist bewusst nicht dabei, weil diese Zeile seit dem Wegfall der Valve bei jedem Request läuft.

**Tool-Ergebnisse laufen ebenfalls durch die externe API** — und mit `log_tool_payload = true` zusätzlich im Klartext ins Serverlog, vor und nach der Maskierung. Die Valve ist eine Debug-Hilfe und gehört im Regelbetrieb aus.

**Über `tool_filter_exclude` ausgeschlossene Tools sehen die externe API gar nicht erst**: Ihr Ergebnis verlässt die eigene Infrastruktur nicht und wird auch nicht im Klartext geloggt. Derselbe Umstand ist das Risiko — es erreicht das LLM unmaskiert. Die Valve gehört deshalb auf Tools beschränkt, deren Ausgabe nachweislich keine personenbezogenen Daten enthalten kann.

Die zweite Kopie in `__metadata__` ist dagegen auf den einzelnen Request begrenzt und wird mit ihm verworfen — kein geteilter Zustand zwischen Nutzern oder Chats.

Zero Data Retention ist eine Kontoeinstellung, kein Request-Parameter. Mit ZDR speichert das Backend keine Zuordnung zwischen Platzhalter und Originalwert — dann liefert `GET /api/status/{job_id}/strings` nichts, und weder die Antwort noch die Tool-Call-Argumente lassen sich zurückübersetzen. Die Maskierung selbst bleibt davon unberührt, sie kommt immer von der API.

---

## Autor & Lizenz

- Filter-Code: `bbojan` — <https://github.com/Bojan227>, Version 1.0.0.
- Dieses Repository: <https://github.com/Matze2010/AnymizeFilter>.
- Lizenz: nicht festgelegt — es liegt keine Lizenzdatei bei.
