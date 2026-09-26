"""Command-line entry point for USISCM file ingest."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path
from typing import Any

from usiscm_ingest.client import UsiscmClient, UsiscmError
from usiscm_ingest.config import load_settings
from usiscm_ingest.microsoft import (
    MicrosoftAuthError,
    MicrosoftTokens,
    clear_tokens,
    device_code_login,
    save_tokens,
)
from usiscm_ingest.package import ingest_source, iter_packages, package_state_key, stamp_name
from usiscm_ingest.state import PackageState, pending_manifest, scan_changes, seed_from_legacy_manifest

logger = logging.getLogger("usiscm_ingest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="usiscm-ingest",
        description=(
            "Watch downloaded estimate files, name drawings automatically, and "
            "ingest them into USIS Construction Management (website catalog + B2). "
            "Ambiguous names still upload; they are logged on the website ingest "
            "tracker for review."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="One-time Microsoft sign-in during the day (saves a refresh token)")
    login.add_argument("--access-token", help="Use an existing Microsoft access token instead of device login")

    sub.add_parser("logout", help="Forget the saved Microsoft session")
    sub.add_parser("whoami", help="Show the signed-in Microsoft / USISCM user")
    sub.add_parser("refresh", help="Silently renew a saved Microsoft session (no prompt; for night jobs)")

    classify = sub.add_parser("classify", help="Classify a zip or folder without uploading")
    classify.add_argument("source", type=Path, help="Zip file or extracted package folder")
    classify.add_argument("--json", dest="json_out", type=Path, help="Write a classification manifest")
    classify.add_argument("--peek-pdf", action="store_true", help="Read first-page PDF text when PyMuPDF is installed")
    classify.add_argument("--work-dir", type=Path, help="Where to extract zips")

    import_cmd = sub.add_parser("import", help="Classify, auto-name, and upload one package to USISCM + B2")
    import_cmd.add_argument("source", type=Path, help="Zip file or extracted package folder")
    import_cmd.add_argument("--project-id", help="USISCM project UUID (skips name matching)")
    import_cmd.add_argument("--dry-run", action="store_true", help="Resolve the project and classify only")
    import_cmd.add_argument("--peek-pdf", action="store_true")
    import_cmd.add_argument("--work-dir", type=Path)
    import_cmd.add_argument("--json", dest="json_out", type=Path)

    watch = sub.add_parser("watch", help="Poll ACCDocs (or another drop folder) and ingest new files")
    watch.add_argument(
        "directory",
        nargs="?",
        type=Path,
        help="Drop folder (default: USISCM_WATCH_DIR or C:\\Users\\CharlesDossett\\DC\\ACCDocs)",
    )
    watch.add_argument("--project-id", help="Force every package onto one project UUID")
    watch.add_argument("--interval", type=int, help="Seconds between scans")
    watch.add_argument("--once", action="store_true", help="Process current packages and exit")
    watch.add_argument("--peek-pdf", action="store_true")
    watch.add_argument("--dry-run", action="store_true")
    watch.add_argument(
        "--move",
        action="store_true",
        help="Move packages out of the drop folder after ingest (do not use on ACCDocs)",
    )
    watch.add_argument(
        "--reprocess",
        action="store_true",
        help="Re-import every file, even if it has not changed",
    )

    specialty = sub.add_parser(
        "specialty-run",
        help="Run queued specialty takeoff jobs one project and one script at a time",
    )
    specialty.add_argument(
        "--once",
        action="store_true",
        help="Drain currently ready projects and exit (for Task Scheduler)",
    )
    specialty.add_argument("--interval", type=int, help="Seconds between polls when not --once")
    specialty.add_argument("--runners", type=Path, help="YAML map of specialty → command or module")
    specialty.add_argument("--max-jobs", type=int, help="Stop after this many projects in one pass")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.command == "login":
        return _cmd_login(args)
    if args.command == "logout":
        return _cmd_logout()
    if args.command == "whoami":
        return _cmd_whoami()
    if args.command == "refresh":
        return _cmd_refresh()
    if args.command == "classify":
        return _cmd_classify(args)
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "watch":
        return _cmd_watch(args)
    if args.command == "specialty-run":
        return _cmd_specialty_run(args)
    parser.error(f"Unknown command {args.command}")
    return 2


def _cmd_login(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = UsiscmClient(settings)
    try:
        app = client.entra_app()
        if args.access_token:
            save_tokens(
                settings.token_path,
                MicrosoftTokens(access_token=args.access_token, expires_at=time.time() + 3600),
            )
            client.settings.ms_access_token = args.access_token
            client.login()
        else:
            print("Sign in with the same Microsoft account you use on usiscm.com.", flush=True)
            tokens = device_code_login(app)
            save_tokens(settings.token_path, tokens)
            client.token = tokens.access_token
            client.auth_mode = "microsoft"
            client.session.headers["Authorization"] = f"Bearer {tokens.access_token}"
        status = client.auth_status()
    except (MicrosoftAuthError, UsiscmError) as exc:
        logger.error("%s", exc)
        return 2
    user = status.get("user") or {}
    print(f"Signed in as {user.get('email') or 'Microsoft user'} (saved {settings.token_path})")
    print("Night jobs will reuse this session. They will not ask you to sign in.")
    return 0 if status.get("authenticated") else 1


def _cmd_refresh() -> int:
    settings = load_settings()
    client = UsiscmClient(settings)
    try:
        client.login(interactive=False)
        if client.uses_ingest_key:
            print("Using USISCM_INGEST_API_KEY — no Microsoft refresh needed.")
            print("Drawing review issues are only posted to the website with a Microsoft session.")
            return 0
        status = client.auth_status()
    except UsiscmError as exc:
        logger.error("%s", exc)
        return 2
    user = status.get("user") or {}
    print(f"Session renewed for {user.get('email') or 'Microsoft user'}")
    return 0 if status.get("authenticated") else 1


def _cmd_logout() -> int:
    settings = load_settings()
    clear_tokens(settings.token_path)
    print(f"Cleared Microsoft session at {settings.token_path}")
    return 0


def _cmd_whoami() -> int:
    settings = load_settings()
    client = UsiscmClient(settings)
    try:
        client.login(interactive=False)
    except UsiscmError as exc:
        logger.error("%s", exc)
        return 2
    if client.uses_ingest_key:
        print(json.dumps({"authenticated": True, "mode": "ingest_key", "night_jobs": True}, indent=2))
        return 0
    try:
        status = client.auth_status()
    except UsiscmError as exc:
        logger.error("%s", exc)
        return 2
    status["mode"] = "microsoft"
    status["night_jobs"] = True
    print(json.dumps(status, indent=2))
    return 0 if status.get("authenticated") else 1


def _cmd_classify(args: argparse.Namespace) -> int:
    manifest = ingest_source(args.source, work_dir=args.work_dir, peek_pdf=args.peek_pdf)
    _print_manifest(manifest)
    if args.json_out:
        manifest.write_json(args.json_out)
        logger.info("Wrote %s", args.json_out)
    return 1 if manifest.errors else 0


def _cmd_import(args: argparse.Namespace) -> int:
    settings = load_settings()
    manifest = ingest_source(args.source, work_dir=args.work_dir or settings.work_dir, peek_pdf=args.peek_pdf)
    _print_manifest(manifest)
    if manifest.errors:
        return 1
    try:
        result = UsiscmClient(settings).import_package(
            manifest,
            project_id=args.project_id,
            dry_run=args.dry_run,
        )
    except UsiscmError as exc:
        logger.error("%s", exc)
        return 2
    print(json.dumps(result.to_dict(), indent=2))
    if args.json_out:
        args.json_out.write_text(json.dumps({"manifest": manifest.to_dict(), "upload": result.to_dict()}, indent=2))
    return 1 if result.errors else 0


def _cmd_watch(args: argparse.Namespace) -> int:
    settings = load_settings()
    drop = args.directory or settings.watch_dir
    if drop is None:
        logger.error("Pass a drop folder or set USISCM_WATCH_DIR")
        return 2
    drop = Path(drop).expanduser()
    if not drop.exists():
        logger.error("Drop folder does not exist: %s", drop)
        return 2
    drop = drop.resolve()
    processed = (settings.processed_dir or drop / "processed").expanduser()
    failed = (settings.failed_dir or drop / "failed").expanduser()
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    interval = args.interval or settings.poll_seconds
    leave_in_place = settings.leave_in_place and not args.move
    client = None if args.dry_run else _client(settings)

    def state_path(package: Path) -> Path:
        return processed / f"{package_state_key(drop, package)}.state.json"

    def run_once() -> int:
        status = 0
        for package in iter_packages(drop):
            logger.info("Scanning %s", package)
            manifest = ingest_source(package, work_dir=settings.work_dir, peek_pdf=args.peek_pdf)
            if manifest.errors:
                _print_manifest(manifest)
                status = 1
                continue
            state = PackageState.load(state_path(package), source=str(package), label=manifest.label)
            seed_from_legacy_manifest(
                state,
                manifest,
                processed / f"{package_state_key(drop, package)}.manifest.json",
            )
            scan = scan_changes(
                manifest,
                state,
                reprocess=args.reprocess,
                settle_seconds=0 if args.reprocess else settings.settle_seconds,
            )
            if scan.settling:
                logger.info("Waiting on %d file(s) still being copied in %s", scan.settling, package.name)
            if not scan.pending:
                logger.info(
                    "No new or changed files in %s (%d unchanged)",
                    package.name,
                    scan.unchanged,
                )
                if not args.dry_run:
                    state.save()
                continue
            pending = pending_manifest(manifest, scan.pending)
            logger.info(
                "Ingesting %d change(s) in %s: %s",
                len(scan.pending),
                package.name,
                ", ".join(f"{delta.reason}:{delta.item.relative_path}" for delta in scan.pending[:20]),
            )
            _print_manifest(pending)
            try:
                if client is None:
                    logger.info("Dry run — not uploading %s", package.name)
                    continue
                result = client.import_package(
                    pending,
                    project_id=args.project_id,
                    dry_run=False,
                )
                print(json.dumps(result.to_dict(), indent=2))
                if result.errors:
                    state.mark_failed(scan.pending, error=json.dumps(result.errors))
                    state.save()
                    status = 1
                    continue
                state.mark_imported(scan.pending, project_id=result.project_id)
                state.save()
                if not leave_in_place:
                    dest = processed / stamp_name(package)
                    if dest.exists():
                        dest = processed / f"{stamp_name(package)}_{id(package)}"
                    shutil.move(str(package), str(dest))
                    pending.write_json(
                        dest.with_suffix(dest.suffix + ".manifest.json") if dest.is_file() else dest / "manifest.json"
                    )
            except (UsiscmError, OSError) as exc:
                logger.error("Failed %s: %s", package, exc)
                status = 1
                try:
                    state.mark_failed(scan.pending, error=str(exc))
                    state.save()
                    if not leave_in_place:
                        shutil.move(str(package), str((settings.failed_dir or drop / "failed") / stamp_name(package)))
                except OSError:
                    pass
        return status

    if args.once:
        return run_once()

    logger.info("Watching %s every %ss", drop, interval)
    while True:
        run_once()
        time.sleep(interval)


def _cmd_specialty_run(args: argparse.Namespace) -> int:
    """Separate from watch. One worker, one project, one script."""
    from usiscm_ingest.specialty_runner import RUNNERS_ENV, ConfiguredScripts, load_runners_file

    settings = load_settings()
    path = args.runners or settings.specialty_runners_path
    try:
        specs = load_runners_file(path)
    except (OSError, ValueError, RuntimeError) as exc:
        logger.error("specialty runners: %s", exc)
        return 2
    if not specs:
        logger.error(
            "No specialty runners configured. Set %s or pass --runners. "
            "See usiscm-specialty-runners.example.yaml",
            RUNNERS_ENV,
        )
        return 2
    scripts = ConfiguredScripts(specs)
    if args.once:
        return _specialty_pass(scripts, max_jobs=args.max_jobs)
    interval = args.interval or settings.poll_seconds
    logger.info("Specialty runner polling every %ss (one project, one script)", interval)
    while True:
        code = _specialty_pass(scripts, max_jobs=args.max_jobs)
        if code == 2:
            return 2
        time.sleep(interval)


def _specialty_pass(scripts: Any, max_jobs: int | None) -> int:
    from usiscm_ingest.specialty_runner import SpecialtyRunnerBusy, run_queue

    try:
        report = run_queue(scripts, max_jobs=max_jobs)
    except SpecialtyRunnerBusy as exc:
        logger.error("%s", exc)
        return 2
    for project in report.projects:
        logger.info(
            "specialty project %s status=%s",
            project.project_key or project.job_id,
            project.status,
        )
    if report.waiting_job_id:
        logger.info(
            "specialty runner waiting on %s (%s)",
            report.waiting_job_id,
            report.waiting_reason,
        )
    elif report.skipped_null_folder:
        logger.info(
            "%d queued job(s) still waiting on folder_path (pending takeoff)",
            report.skipped_null_folder,
        )
    if report.blocked_job_id:
        logger.error("specialty queue blocked on %s: %s", report.blocked_job_id, report.blocked_reason)
        return 2
    return 1 if report.failed else 0


def _client(settings) -> UsiscmClient:
    return UsiscmClient(settings)


def _print_manifest(manifest) -> None:
    print(f"Package: {manifest.label}")
    print(f"Source:  {manifest.source}")
    print(f"Root:    {manifest.root_dir}")
    print("Counts:  " + ", ".join(f"{k}={v}" for k, v in manifest.counts.items()))
    for item in manifest.files:
        extra = f" [{item.sheet_number}]" if item.sheet_number else ""
        print(f"  {item.category.value:18} {item.confidence:4.2f}  {item.relative_path}{extra}")
        for reason in item.reasons:
            print(f"    - {reason}")
    for error in manifest.errors:
        print(f"ERROR: {error}")


if __name__ == "__main__":
    sys.exit(main())
