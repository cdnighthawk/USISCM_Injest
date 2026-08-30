# USISCM Ingest

Server-side script that classifies files in an estimate package and imports them into [USIS Construction Management](https://www.usiscm.com).

GC offices do **not** share one package layout. A Turner progress-print zip, a numbered Swinerton folder tree, and a flat Webcor dump are all valid. The importer never requires a project-name pattern, `Progress Print` suffix, or revision scheme. It scores each file as:

| Category | Typical signals (none required) |
| --- | --- |
| `drawing` | Sheet numbers (`A-101`, `S2.01`), `.dwg`/`.dxf`, folders like Drawings/Plans |
| `spec` | Project manual / specification wording, CSI section numbers (`09 29 00`) |
| `bid_instructions` | ITB, instructions to bidders, bid form, Division 00 |
| `addenda` | Addendum / bulletin / ASI |
| `report` | Geotech, soils, survey, environmental |
| `schedule` | Baseline / CPM / `.mpp` / `.xer` |
| `other` | Everything else that still should be stored |

## Install

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -e ".[dev]"
```

PDF first-page text is optional and only used with `--peek-pdf`:

```bash
pip install -e ".[pdf]"
```

Copy `.env.example` to `.env`. Sign in with **Microsoft** (the same SSO used on [usiscm.com](https://www.usiscm.com)) — there is no email/password login for this script.

```bash
usiscm-ingest login
```

That prints a Microsoft device code. Open the URL, sign in with your USIS work account, then the token is stored under `~/.config/usiscm-ingest/ms_tokens.json`. Tenant and client id are read from `/auth/microsoft/start` unless you set `USISCM_MS_TENANT_ID` / `USISCM_MS_CLIENT_ID`.

```bash
usiscm-ingest whoami
usiscm-ingest logout
```

## Classify a package

Works on a zip **or** an already-extracted folder:

```bash
usiscm-ingest classify "/data/drops/Kaiser Permanente San Rafael - Progress Print _5.zip"
usiscm-ingest classify "/data/drops/Sutter Health Oakland MOB"
usiscm-ingest classify ./package --json manifest.json
```

## Import into USISCM

```bash
# Match a project by the package label (zip/folder name, with optional suffixes stripped)
usiscm-ingest import /data/drops/some-bid-set.zip

# Or pin the project UUID
usiscm-ingest import /data/drops/some-bid-set.zip --project-id 00000000-0000-0000-0000-000000000000

# Review the match without uploading
usiscm-ingest import /data/drops/some-bid-set.zip --dry-run
```

Uploads use your Microsoft token the same way the USIS desktop app does:

- Drawing PDFs/images → `POST /api/v1/projects/{id}/drawings`
- Spec PDFs → `POST /api/v1/projects/{id}/spec-book/import` plus the documents ingest API
- Other categories → `POST /api/documents` (optional `USISCM_INGEST_API_KEY` if your site requires the machine ingest key for that route)

## Watch a drop folder on the server

Point this at the directory your office copies packages into:

```bash
export USISCM_WATCH_DIR=/data/estimate-drops
usiscm-ingest watch --once          # process what is there now
usiscm-ingest watch                 # poll until stopped
```

Finished packages move to `processed/` (or `USISCM_PROCESSED_DIR`). Failures move to `failed/`. A JSON manifest is written next to each moved package.

## Optional hint file

If one office uses unusual folder words, copy `usiscm-ingest.example.yaml` and add those words. Do not encode a single GC's zip naming as a rule.

## Tests

```bash
pytest
```
