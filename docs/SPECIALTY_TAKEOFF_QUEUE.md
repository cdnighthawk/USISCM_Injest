# Specialty Takeoff Queue — `usis.specialty_takeoff.v1`

Canonical contract for the nine specialty takeoff bots. **This repository
(USISCM Ingest) owns enqueue.** Specialty bots own claim and run. The tray
must not poll or drive this queue.

Enqueue is a first-class call in `usiscm_ingest.specialty_takeoff`, wired from
`UsiscmClient.import_package` after a clean catalog + upload. It is not a
monkey-patch of `C:\usis-cm\ingestion_agent.py`.

## Ownership

| Concern | Owner |
|---|---|
| Enqueue after a clean ingest batch (`imported > 0`, no upload errors) | **This app** — `enqueue_specialty_takeoff` from `UsiscmClient.import_package` (`usiscm-ingest import` and `watch`) |
| Patch `folder_path` later and set `ready_for_takeoff` | Whoever learns the CM estimate folder (not this app at enqueue time). Helper: `patch_job_folder_path` |
| Claim + run specialty takeoff | Specialty bots (optional helpers in this module) |
| Tray / connector | Unrelated — must not poll or drive this queue |

## When a job is written

Grain is **one job per `import_package` call**: one project, and the files in
that run.

`import_package` uploads drawings (website catalog row, native B2 or
`POST /api/drawings`, then ack) and documents (`POST /api/documents` or
`POST /api/v1/ingest/files`). After those calls are tallied:

- At least one file has `imported > 0` and the batch recorded **no** upload errors → one `queued/{job_id}.json`.
- Dry run, zero stored files, or any upload error → no job. `watch` retries a failed batch; the job is written once a later run is clean.
- A naming review (`kind=naming`) still counts as success. The file is already stored.
- Enqueue I/O failures are logged. They do not change the upload result and do not fail the watcher.

`folder_path` is **null** at enqueue. This app does not invent an estimate
folder or a `Y:\` root. `source_paths` are the local files that were stored
(often under the ACCDocs watch directory). Those are not `folder_path`.

When a folder is known later, `patch_job_folder_path` writes it on the same
JSON file and sets `status` to `ready_for_takeoff`. Consumers wait until
`folder_path` is set. The claim helper refuses a null path.

## Runtime paths (Windows)

```
C:\usis-cm\data\queues\specialty_takeoff\
  queued\{job_id}.json       # written by this app on enqueue
  processing\{job_id}.json   # claimed by a specialty consumer
  done\{job_id}.json         # success
  failed\{job_id}.json       # failure (+ error)
```

Per-job JSON files. Not a tray-owned `queue.jsonl`.

Override the root with `USIS_SPECIALTY_TAKEOFF_QUEUE` (tests and non-Windows hosts).

## Schema id

`schema`: **`usis.specialty_takeoff.v1`**

Consumers should ignore unknown fields. Floor on every job file:

| Field | Type | Notes |
|---|---|---|
| `schema` | string | `"usis.specialty_takeoff.v1"` |
| `job_id` | string | uuid4; also the filename |
| `project_key` | string \| null | Project key, else project number, else project name, else package label |
| `cm_ids` | object | `project_id` (USISCM project id when known) and `estimate_id` (only if the project payload has one). Extra keys are preserved. |
| `source_paths` | string[] | Local paths stored in this batch |
| `specialties` | string[] | `["all"]` or any of the nine slugs: `lockers`, `concrete`, `door_spec`, `room_interiors`, `wall_protection`, `partitions`, `fec`, `millwork`, `bathroom_accessories`. Claim expands `all`. |
| `folder_path` | string \| null | CM estimate folder. **Null at enqueue.** |
| `estimate_folder` | string \| null | Deprecated alias of `folder_path` |
| `enqueued_at` | string | ISO-8601 UTC with `Z` |
| `status` | string | `queued` → (`ready_for_takeoff` once the folder is patched) → `processing` → `done` \| `failed` |

Optional fields this app sets:

- `trigger`: `"ingest_ok"`
- `file_ids`: drawing / document ids returned by the website, when present
- `error`, `claimed_at`, `claimed_by`, `finished_at`: null until a consumer sets them
- `source`: `"usiscm_ingest"`
- `batch_id`: ingest batch id for that `import_package` call

## Claim convention

1. Consumer lists `queued\*.json` and skips jobs with a null `folder_path` (`iter_ready`).
2. **Claim** = exclusive move `queued\{job_id}.json` → `processing\{job_id}.json`, set `status=processing`, `claimed_at`, `claimed_by`. `specialties=["all"]` expands to the nine slugs. `folder_path` wins over `estimate_folder`.
3. On success: move to `done\{job_id}.json`, `status=done`, `finished_at`.
4. On failure: move to `failed\{job_id}.json`, `status=failed`, `error=…`, `finished_at`.
5. Filesystem rename plus a JSON rewrite. No PowerShell. This repo does not install or relaunch a tray.

```python
from usiscm_ingest.specialty_takeoff import claim_job, iter_ready, mark_done, mark_failed

for path, job in iter_ready():
    claimed = claim_job(job["job_id"], claimed_by="lockers")
    try:
        # run specialty takeoff using claimed["folder_path"] and claimed["source_paths"]
        mark_done(claimed["job_id"])
    except Exception as exc:
        mark_failed(claimed["job_id"], error=str(exc))
```

## Example job (`queued`)

See [specialty_takeoff.example.json](specialty_takeoff.example.json).
