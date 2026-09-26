# USISCM Ingest

Watch files Autodesk Desktop Connector (or another drop folder) downloads, **name drawings automatically**, and ingest them into [USIS Construction Management](https://www.usiscm.com) plus Backblaze B2.

This is the same upload path the USIS PDF app uses: the website stores the catalog row, the PDF is POSTed from this machine to native B2, then the website is acked. Bytes do not go through Render.

Multi-page **drawing** PDFs are split on this PC into one PDF per sheet before native B2 upload. Sheet number, title, discipline, set, and revision are read from that sheet's title block (and, for a file that is already one sheet, from the filename and folder, such as `A1-001_BCK-1.pdf` under `Architectural/Permit-Set/`). Package and form tokens (`PKG1`, `ADD01`, `NO.4`, `W9`) are not sheet numbers. If the name is missing or does not look like a real sheet id, the sheet **still uploads** and a review item is posted to `POST /api/v1/ingest/errors` so it appears in the CM ingest tracker when someone opens the app.

Only real drawing PDFs are sheet-split. Specs, specifications, project manuals, addenda, bid forms, W-9s, RFP/RFQ, manuals, and combined bid sets stay the original multi-page PDF and go to the documents API only. A name or path like `Pali_CHS_JAN_2025_Specs` is a spec book even when it sits in a Drawings folder. They are not sheet-split and they do not get drawing rows. A single sheet whose filename is a real sheet id, such as `A-101 Wall Specifications`, is still a drawing.

GC offices do **not** share one package layout. A Turner progress-print zip, a numbered Swinerton folder tree, and a flat Webcor dump are all valid. The importer never requires a project-name pattern, `Progress Print` suffix, or revision scheme. It scores each file as:

| Category | Typical signals (none required) |
| --- | --- |
| `drawing` | Sheet numbers (`A-101`, `S2.01`), `.dwg`/`.dxf`, folders like Drawings/Plans |
| `spec` | `Specs`, specification, project manual, CSI section numbers (`09 29 00`) |
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

Sheet split uses PyMuPDF, which is installed with the package, and falls back to pypdf for a page MuPDF cannot extract. `--peek-pdf` / `USISCM_SHEET_AI` still control first-page classification text and the website title-block AI.

Copy `.env.example` to `.env` on the **server**. Night jobs do not open a Microsoft login.

On this server, Autodesk Desktop Connector already downloads ACC projects to:

`C:\Users\CharlesDossett\DC\ACCDocs`

That path is the default watch directory. Ingest never moves files out of ACCDocs (doing so would look like a delete to Desktop Connector). Per-file ingest state is written under `C:\Users\CharlesDossett\DC\USISCM-ingest`.

### Unattended night runs (recommended)

Sign in once during the day so a Microsoft refresh token is saved. That is what USISPdfApp uses to mint native B2 URLs and to write ingest issues onto the website.

```bash
usiscm-ingest login
```

Optionally also set `USISCM_INGEST_API_KEY` (same key as Autodesk ingest: `CM_INGEST_API_KEY` / `CM_API_KEY`). With a Microsoft session, drawings and documents both use the website session routes. The ingest key is the night fallback when Microsoft is down: it still registers JSON only, then this PC uploads the file to native B2.

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

Drawings and documents (specs, addenda, reports, other files) use the same native B2 path. File bytes never go through Render. Multipart `POST /api/drawings` and `POST /api/documents` are rejected by the website (`410` `AGENT_MULTIPART_FORBIDDEN`).

Microsoft session:

1. Split each multi-page drawing PDF into one PDF per sheet. Name each sheet from its own text. Leave specs, addenda, bid forms, W-9s, manuals, and combined bid sets whole, on the documents path.
2. Auto-name any sheet the title block did not fill (optional website sheet-identity AI)
3. `POST /api/v1/jobs/{jobId}/drawings` or `POST /api/v1/jobs/{jobId}/documents` (metadata only, one row per sheet or document)
4. Native `b2_upload_file` from this PC
5. `POST /api/v1/drawings/{id}/ack-file` or `POST /api/v1/documents/{id}/ack-file`

`split_pages` stays false in the ingest-key JSON. The split already happened here; the website must not split the file again, and file bytes still do not go through Render.

If the job's estimate folder **already exists**, pass `--estimate-folder` (or a project `folder_path` / `estimate_folder` the website already returned). Each sheet is also copied to `<folder>\02_Processed\drawings`. A missing folder is skipped. This app does not create `Y:\Estimates` or any other estimate root.

Ingest API key (no Microsoft session):

1. `POST /api/drawings` or `POST /api/documents` with JSON metadata
2. Native `b2_upload_file` from this PC
3. `POST /api/drawings/{id}/ack-file` or `POST /api/documents/{id}/ack-file`

## Watch downloaded files

```bash
usiscm-ingest watch --once
usiscm-ingest watch
```

`--reprocess` forces every selected file again. `--package` limits the scan to one ACC project (folder name or path fragment). `--move` is only for a throwaway zip drop folder that is **not** ACCDocs.

### Reprocess one package after install

Install this version on the data server, then force one job (for example ACC project `26092`) through sheet split. Other projects are not touched:

```bash
usiscm-ingest watch --once --reprocess --package 26092
```

That re-reads the ACCDocs package whose path contains `26092`, sheet-splits multi-page drawing PDFs only, uploads each sheet to native B2, and uploads specs, project manuals, addenda, W-9s, RFPs, and other non-drawings as the original multi-page file on documents only.

A long `watch`, `import`, `reprocess`, or specialty run does not ask Charles to sign in. After the one-time daytime `usiscm-ingest login`, the saved refresh token silently renews the access token before each website call when it is inside 15 minutes of expiry (or inside one upload timeout, whichever is longer). Catalog create, B2 mint, ack, and document posts that return `401` refresh once and retry that same request. Device-code login is not used. If the refresh token is missing or Microsoft rejects it, the job logs that and exits. The ingest API key does not expire; a `401` on that key retries only if `USISCM_INGEST_API_KEY` was changed in the environment.

Sheet split is per page. Each sheet is rewritten so unused images, fonts, and other objects from the rest of the set are not stored in that file. A one-page sheet must stay far smaller than the source PDF. Sheet titles used as filenames are Windows-safe: newlines, tabs, other control characters, and `<>:"/\|?*` are removed so a title cannot create another folder or an illegal path. A name that collides after that cleanup gets the page index. If MuPDF hits a stack overflow on one page, that page is extracted with pypdf (or pikepdf when it is installed) and rewritten the same way. The other pages still upload. A page that cannot be extracted as a compact one-page PDF is logged by page number and omitted. The run reports those pages, uploads the sheets that did split, and does not send the original multi-page PDF as one drawing.

To also drop the sheets into an estimate folder that is already on disk:

```bash
usiscm-ingest watch --once --reprocess --package 26092 --estimate-folder "Y:\Estimates\26092"
```

If `Y:\Estimates\26092` does not exist, the copy is skipped and nothing is created under `Y:\Estimates`.

A one-folder import (no watch state) does the same split:

```bash
usiscm-ingest import "C:\Users\CharlesDossett\DC\ACCDocs\<hub>\26092" --project-id <job-uuid>
```

Reprocess uploads new sheet rows. It does not delete catalog rows already on the Drawings page. After `26092` finishes, remove the old whole-file drawing rows (and any addendum, W-9, or spec that landed there) in CM. What remains on Drawings should be one row per sheet. New non-drawings are on Documents.

State for that package lives at `C:\Users\CharlesDossett\DC\USISCM-ingest\processed\<hub>__<project>.state.json`. `--reprocess --package 26092` ignores the "already imported" marks for that package only.

## Review issues on the website

Open the CM ingest tracker (same page as mass ingest errors). Items from this tool have `source=usiscm_ingest`:

- `kind=naming` — automatic drawing name is missing or does not look like A-100
- `kind=drawing` / `kind=document` — upload or B2 ack failed

The file is already on B2 and in the project when the issue is only a naming review.

## Optional hint file

If one office uses unusual folder words, copy `usiscm-ingest.example.yaml` and add those words. Do not encode a single GC's zip naming as a rule.

## Specialty takeoff

After a clean upload, `UsiscmClient.import_package` writes one `usis.specialty_takeoff.v1` job for that project batch. Both `import` and `watch` use that method. They do not run specialty scripts. The file is `queued/{job_id}.json` under `C:\usis-cm\data\queues\specialty_takeoff` (`USIS_SPECIALTY_TAKEOFF_QUEUE` overrides the root). A queue write failure is logged and does not fail the upload.

`folder_path` is null at enqueue. This app does not invent an estimate-folder path. The job stays queued (pending takeoff) until a later patch sets `folder_path` and `status=ready_for_takeoff`.

Run the scripts with **one** separate worker. It takes the oldest queued project, expands `["all"]` to the nine specialties, and runs each plugged-in script to completion before the next script and before the next project. A null `folder_path` holds the queue until that path is patched.

```bash
usiscm-ingest specialty-run --once --runners usiscm-specialty-runners.example.yaml
usiscm-ingest specialty-run
```

Point `USIS_SPECIALTY_RUNNERS` at a copy of `usiscm-specialty-runners.example.yaml` with a command or `module:function` for each slug. On Windows, schedule `usiscm-ingest specialty-run --once` as a single task. Do not start it from `watch`. The tray does not drive this queue.

Contract: [docs/SPECIALTY_TAKEOFF_QUEUE.md](docs/SPECIALTY_TAKEOFF_QUEUE.md). Code: `usiscm_ingest.specialty_takeoff` (enqueue) and `usiscm_ingest.specialty_runner` (serial run).

## Tests

```bash
pytest
```
