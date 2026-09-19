# USISCM Ingest

Watch files Autodesk Desktop Connector (or another drop folder) downloads, **name drawings automatically**, and ingest them into [USIS Construction Management](https://www.usiscm.com) plus Backblaze B2.

This is the same upload path the USIS PDF app uses: the website stores the catalog row, the PDF is POSTed from this machine to native B2, then the website is acked. Bytes do not go through Render.

The PDF-app drawing namer is automated here. Sheet number, title, discipline, set, and revision are read from the filename and folder (`A1-001_BCK-1.pdf` under `Architectural/Permit-Set/`). If the name is missing or does not look like a real sheet id, the file **still uploads** and a review item is posted to `POST /api/v1/ingest/errors` so it appears in the CM ingest tracker when someone opens the app.

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

PDF first-page text and title-block crops are optional (`--peek-pdf` / `USISCM_SHEET_AI`):

```bash
pip install -e ".[pdf]"
```

Copy `.env.example` to `.env` on the **server**. Night jobs do not open a Microsoft login.

On this server, Autodesk Desktop Connector already downloads ACC projects to:

`C:\Users\CharlesDossett\DC\ACCDocs`

That path is the default watch directory. Ingest never moves files out of ACCDocs (doing so would look like a delete to Desktop Connector). Per-file ingest state is written under `C:\Users\CharlesDossett\DC\USISCM-ingest`.

### Unattended night runs (recommended)

Sign in once during the day so a Microsoft refresh token is saved. That is what USISPdfApp uses to mint native B2 URLs and to write ingest issues onto the website.

```bash
usiscm-ingest login
```

Optionally also set `USISCM_INGEST_API_KEY` (same key as Autodesk ingest: `CM_INGEST_API_KEY` / `CM_API_KEY`) so documents can use the bearer ingest API.

```bash
# /etc/usiscm-ingest.env
USISCM_BASE_URL=https://www.usiscm.com
USISCM_WATCH_DIR=C:\Users\CharlesDossett\DC\ACCDocs
```

```bash
usiscm-ingest watch --once
```

Or enable the systemd timer in `deploy/usiscm-ingest.timer`.

```bash
usiscm-ingest whoami
usiscm-ingest refresh
usiscm-ingest logout
```

If Microsoft is unavailable, `USISCM_INGEST_API_KEY` still uploads through the bearer ingest API (one file at a time). Review issues are only posted to the website with a Microsoft session.

## Classify a package

Works on a zip **or** an already-extracted folder:

```bash
usiscm-ingest classify "/data/drops/Kaiser Permanente San Rafael - Progress Print _5.zip"
usiscm-ingest classify "/data/drops/Sutter Health Oakland MOB"
usiscm-ingest classify ./package --json manifest.json
```

## Import into USISCM + B2

```bash
usiscm-ingest import /data/drops/some-bid-set.zip
usiscm-ingest import /data/drops/some-bid-set.zip --project-id 00000000-0000-0000-0000-000000000000
usiscm-ingest import /data/drops/some-bid-set.zip --dry-run
```

Drawings:

1. Auto-name from path (optional website sheet-identity AI if PyMuPDF is installed)
2. `POST /api/v1/jobs/{jobId}/drawings` (metadata only)
3. Native `b2_upload_file` from this PC
4. `POST /api/v1/drawings/{id}/ack-file`

Documents go to `POST /api/documents` (ingest key) or `POST /api/v1/ingest/files` (Microsoft session).

## Watch downloaded files

```bash
usiscm-ingest watch --once
usiscm-ingest watch
```

`--reprocess` forces every file again. `--move` is only for a throwaway zip drop folder that is **not** ACCDocs.

## Review issues on the website

Open the CM ingest tracker (same page as mass ingest errors). Items from this tool have `source=usiscm_ingest`:

- `kind=naming` — automatic drawing name is missing or does not look like A-100
- `kind=drawing` / `kind=document` — upload or B2 ack failed

The file is already on B2 and in the project when the issue is only a naming review.

## Optional hint file

If one office uses unusual folder words, copy `usiscm-ingest.example.yaml` and add those words. Do not encode a single GC's zip naming as a rule.

## Tests

```bash
pytest
```
