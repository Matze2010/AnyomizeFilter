# anymize API — Anonymisierung

> Base URL: `https://app.anymize.ai/api/v1/llm`
> Anonymous Base URL: `https://app.anymize.ai/api/v1/llm-anonymous`
> Auth: `Authorization: Bearer YOUR_API_KEY`

Text und Dateien anonymisieren, anonymer LLM-Chat mit automatischer De-Anonymisierung.

---

# Text-Anonymisierung

Personenbezogene Daten aus Text automatisch maskieren.

## Text Anonymization

```
POST /api/anonymize
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| text | string | Yes | - | Text to anonymize |
| language | string | No | "de" | Language code (de, en, fr, es, it) |

```bash
curl -X POST https://app.anymize.ai/api/anonymize \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Max Mustermann wohnt in Berlin", "language": "de"}'
```

### Response (202)

```json
{
  "job_id": "job_abc123def456",
  "status": "processing",
  "message": "Anonymization job created"
}
```

## OCR + Anonymization

```
POST /api/ocr
```

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| file | File | Yes | - | PDF, PNG, JPG, or TIFF file |
| language | string | No | "de" | Language code |

```bash
curl -X POST https://app.anymize.ai/api/ocr \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "file=@document.pdf" \
  -F "language=de"
```

---

# Datei-Anonymisierung

Dokumente und Bilder per OCR verarbeiten und automatisch anonymisieren.

## Endpoint

```
POST /api/ocr
```

## Supported Formats

PDF, PNG, JPG, TIFF

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| file | File | Yes | - | Document or image file |
| language | string | No | "de" | Language code |

> Note: Use multipart/form-data for file uploads.

## Example

```bash
curl -X POST https://app.anymize.ai/api/ocr \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -F "file=@document.pdf" \
  -F "language=de"
```

## Processing Pipeline

1. OCR extracts text from the document
2. Anonymization pipeline detects and replaces PII
3. Check status with GET /api/status/{job_id}

---

# Anonymer Chat

Chat mit automatischer Anonymisierung in einem Schritt.

## Endpoint

```
POST /api/v1/llm-anonymous/chat/completions
```

Base URL: `https://app.anymize.ai/api/v1/llm-anonymous`

## Flow

1. Input text with PII
2. Anonymization pipeline replaces PII with placeholders
3. LLM processes anonymized text
4. De-anonymization restores original values in response

## Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| model | string | Yes | - | Model ID (e.g. fountain-1.0) |
| messages | array | Yes | - | Chat messages array |
| language | string | No | "de" | Language for anonymization |
| stream | boolean | No | false | Enable streaming |

## Example

```bash
curl -X POST https://app.anymize.ai/api/v1/llm-anonymous/chat/completions \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "model": "fountain-1.0",
    "messages": [{"role": "user", "content": "Schreibe eine E-Mail an Max Mustermann"}],
    "language": "de"
  }'
```

> Zero Data Retention (ZDR) is configured in your account settings, not per request.

## Placeholder System Prompt (auto-injected)

The anonymous endpoint automatically appends this to your system prompt.
You do NOT need to add it yourself. Your own system prompt is preserved.

```text
## CRITICAL RULE: ANONYMIZATION PLACEHOLDERS

The user's messages contain anonymized placeholders shaped [[<Type>-<HASH>]],
where <Type> is the PII category (person, email, iban, telephone_number,
address, ...) and <HASH> is a short random code.

FORBIDDEN:
- Writing "[Name anonymisiert]" or "[anonymized]"
- Describing WHAT the placeholder is instead of USING it
- Writing a double-bracket token that was NOT in the user's message

REQUIRED:
- Copy each placeholder character-for-character from the user's message
- Use it inline like the real value: "Herr <placeholder> hat..."
- Need a value the user never gave you? Use SINGLE brackets ([Name]).

WHY: After your response, placeholders get replaced with real values.
```

## Credits

Anonymous chat uses double credits: LLM token credits + anonymization word credits (1 word = 1 credit).

---

# De-Anonymisierung

Maskierte Platzhalter wieder durch Originaldaten ersetzen.

## Endpoint

```
POST /api/deanonymize
```

## Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| text | string | Yes | Anonymized text containing [[Type-HASH]] placeholders |

## Example

```bash
curl -X POST https://app.anymize.ai/api/deanonymize \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"text": "Sehr geehrter [[Person-QSEZB6]], Ihre Adresse [[Adress-NT9DQE]] wurde aktualisiert."}'
```

### Response (200)

```json
{
  "text": "Sehr geehrter Max Mustermann, Ihre Adresse Berliner Str. 42, 10115 Berlin wurde aktualisiert.",
  "replacements": 2
}
```

## Limitations

- The original anonymization job must still exist
- Not available when Zero Data Retention (ZDR) is enabled in account settings
- Only the user who created the job can de-anonymize

---

# Hash-Paare

Zuordnung zwischen Platzhaltern und Originaldaten abrufen.

## Endpoint

`GET /api/status/{jobId}/strings`

Returns the mapping between original values and anonymization placeholders.

## Path Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| jobId | string | Yes | The job ID from the original request |

## Response

```json
{
  "job_id": "job_abc123def456",
  "hash_pairs": [
    { "original": "Max Mustermann", "hash": "[PERSON-1]", "prefix_name": "Person", "placeholder": "PERSON-1" },
    { "original": "Berlin", "hash": "[LOCATION-1]", "prefix_name": "Location", "placeholder": "LOCATION-1" },
    { "original": "Acme GmbH", "hash": "[ORGANIZATION-1]", "prefix_name": "Organization", "placeholder": "ORGANIZATION-1" }
  ],
  "total": 3
}
```

## Entity Types

Person, Location, Organization, Date, Email, Phone, Address, IBAN, ID

## Example

```bash
curl https://app.anymize.ai/api/status/job_abc123def456/strings \
  -H "Authorization: Bearer YOUR_API_KEY"
```

> **Note:** Hash pairs are not available when Zero Data Retention (ZDR) is enabled in your account settings.
