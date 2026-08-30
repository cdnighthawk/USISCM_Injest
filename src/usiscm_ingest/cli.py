"""Command-line entry point for USISCM file ingest."""

from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
from pathlib import Path

from usiscm_ingest.client import UsiscmClient, UsiscmError
from usiscm_ingest.config import load_settings
from usiscm_ingest.package import ingest_source, iter_packages, package_state_key, stamp_name
from usiscm_ingest.state import PackageState, pending_manifest, scan_changes, seed_from_legacy_manifest

logger = logging.getLogger("usiscm_ingest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="usiscm-ingest",
        description=(
            "Classify files in an estimate package (zip or folder) and import them "
            "into USIS Construction Management. Packages are not assumed to follow "
            "any one GC's naming — files are categorized as drawings, specs, bid "
            "instructions, addenda, reports, schedules, or other."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    classify = sub.add_parser("classify", help="Classify a zip or folder without uploading")
    classify.add_argument("source", type=Path, help="Zip file or extracted package folder")
    classify.add_argument("--json", dest="json_out", type=Path, help="Write a classification manifest")
    classify.add_argument("--peek-pdf", action="store_true", help="Read first-page PDF text when PyMuPDF is installed")
    classify.add_argument("--work-dir", type=Path, help="Where to extract zips")

    import_cmd = sub.add_parser("import", help="Classify and upload one package to USISCM")
    import_cmd.add_argument("source", type=Path, help="Zip file or extracted package folder")
    import_cmd.add_argument("--project-id", type=int, help="USISCM project id (skips name matching)")
    import_cmd.add_argument("--dry-run", action="store_true", help="Resolve the project and classify only")
    import_cmd.add_argument("--peek-pdf", action="store_true")
    import_cmd.add_argument("--work-dir", type=Path)
    import_cmd.add_argument("--json", dest="json_out", type=Path)

    watch = sub.add_parser("watch", help="Poll ACCDocs (or another drop folder) and import new packages")
    watch.add_argument(
        "directory",
        nargs="?",
        type=Path,
        help="Drop folder (default: USISCM_WATCH_DIR or C:\\Users\\CharlesDossett\\DC\\ACCDocs)",
    )
    watch.add_argument("--project-id", type=int, help="Force every package onto one project")
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

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    if args.command == "classify":
        return _cmd_classify(args)
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "watch":
        return _cmd_watch(args)
    parser.error(f"Unknown command {args.command}")
    return 2


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
        result = _client(settings).import_package(
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


def _client(settings) -> UsiscmClient:
    if not settings.email or not settings.password:
        raise SystemExit("Set USISCM_EMAIL and USISCM_PASSWORD (see .env.example)")
    return UsiscmClient(settings.base_url, settings.email, settings.password)


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
