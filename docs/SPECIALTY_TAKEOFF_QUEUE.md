# Specialty Takeoff Queue — `usis.specialty_takeoff.v1`

Canonical contract for the nine specialty takeoff scripts. **This repository
(USISCM Ingest) owns enqueue and the serial runner.** Each specialty's script
is plugged in from outside this repo. The tray must not poll or drive this
queue.

Enqueue is `usiscm_ingest.specialty_takeoff`, called from
`UsiscmClient.import_package` after a clean catalog + upload. It does not run
scripts and does not block watch. The worker is a separate process:
`usiscm-ingest specialty-run`.

## Ownership

| Concern | Owner |
|---|---|
| Enqueue after a clean ingest batch (`imported > 0`, no upload errors) | **This app** — `enqueue_specialty_takeoff` from `UsiscmClient.import_package` (`usiscm-ingest import` and `watch`) |
| Run every specialty for a project, one script at a time, one project at a time | **This app** — `usiscm-ingest specialty-run` (`usiscm_ingest.specialty_runner`). Not inside watch. |
| Specialty script bodies | External commands or `module:function` plugs (`USIS_SPECIALTY_RUNNERS`) |
| Patch `folder_path` later and set `ready_for_takeoff` | Whoever learns the CM estimate folder (not this app at enqueue time). Helper: `patch_job_folder_path` |
| Tray / connector | Unrelated — must not poll or drive this queue |

## Serial runner

After ingest, schedule **one** worker:

```bash
usiscm-ingest specialty-run --once --runners path\to\runners.yaml
usiscm-ingest specialty-run
```

`--once` drains ready projects and exits (Windows Task Scheduler). Without
`--once` the process polls and stays up. `deploy/usiscm-ingest-specialty.service`
is that loop. Do not start a second copy: the queue root holds `runner.lock`
for the whole drain.

Rules:

1. Finish the current project before claiming the next. An existing `processing/` job is resumed first.
2. Expand `specialties: ["all"]` to the nine slugs and run those scripts **one after another**. The next script starts only after the previous process returns.
3. The next project is the oldest queued job (`enqueued_at`, then `job_id`). Finish it before looking at a newer one.
4. `folder_path` null → that job waits in `queued/` (pending takeoff). The worker does not claim it, does not invent a `Y:\` (or any other) estimate folder, and does not start a newer project ahead of it. When the path is patched, preferred status is `ready_for_takeoff`. A queued job that already has `folder_path` is ready.
5. If that head job's slugs are not all configured, the queue blocks. Later projects are not started. The job stays in `queued/`.
6. If one script fails, the rest of that project's scripts still run, in order. The job is then `failed`. The next project starts only after that.

Plug-in file: [usiscm-specialty-runners.example.yaml](../usiscm-specialty-runners.example.yaml).

```yaml
runners:
  lockers:
    command: ["python", "C:\\usis-cm\\specialties\\lockers.py", "--folder", "{folder_path}"]
  concrete:
    module: estimating_specialties.concrete:run
```

`module` calls `function(specialty, job)`. `command` is blocking (`subprocess.run`) with `{folder_path}`, `{project_key}`, `{job_id}`, `{project_id}`, `{estimate_id}` filled from the job. The same values are in `USIS_SPECIALTY_*` environment variables. `USIS_SPECIALTY_SOURCE_PATHS` is the ingested files, separated by the OS path separator.

## When a job is written

Grain is **one job per `import_package` call**: one project, and the files in
that run.

`import_package` uploads drawings and documents the same way: website catalog
row (metadata only), native B2 from this PC, then ack. Microsoft uses
`POST /api/v1/jobs/{id}/drawings` and `POST /api/v1/jobs/{id}/documents`.
The ingest key uses JSON `POST /api/drawings` and `POST /api/documents`.
After those calls are tallied:

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
- `error`, `claimed_at`, `claimed_by`, `finished_at`: null until the runner sets them
- `source`: `"usiscm_ingest"`
- `batch_id`: ingest batch id for that `import_package` call
- `specialty_results`: per-script `{specialty, status, started_at, finished_at, error}` written while the job is in `processing/`

## Claim convention

The serial runner is the consumer. It uses the helpers below. Do not also run per-specialty bots against the same queue.

1. List `queued\*.json` and sort by `enqueued_at`. The oldest job is the only candidate.
2. If its `folder_path` is null, stop (pending takeoff). Otherwise claim it: move `queued\{job_id}.json` → `processing\{job_id}.json`, set `status=processing`, `claimed_at`, `claimed_by=usiscm_ingest.specialty_runner`. `specialties=["all"]` expands to the nine slugs. `folder_path` wins over `estimate_folder`.
3. Run each slug's script, one at a time. Append `specialty_results` after each script returns.
4. On success of every script: move to `done\{job_id}.json`, `status=done`, `finished_at`.
5. If any script failed: move to `failed\{job_id}.json`, `status=failed`, `error=…`, `finished_at`. Then the next project may start.
6. Filesystem rename plus a JSON rewrite. No PowerShell. This repo does not install or relaunch a tray.

```python
from usiscm_ingest.specialty_runner import run_queue
from usiscm_ingest.specialty_runner import ConfiguredScripts, load_script_specs

specs = load_script_specs(path_to_runners_yaml)
run_queue(ConfiguredScripts(specs))
```

## Example job (`queued`)

See [specialty_takeoff.example.json](specialty_takeoff.example.json).
