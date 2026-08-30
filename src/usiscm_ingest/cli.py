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
from usiscm_ingest.microsoft import (
    MicrosoftAuthError,
    MicrosoftTokens,
    clear_tokens,
    device_code_login,
    save_tokens,
)
from usiscm_ingest.package import ingest_source, iter_packages, stamp_name

logger = logging.getLogger("usiscm_ingest")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="usiscm-ingest",
        description=(
            "Classify files in an estimate package (zip or folder) and import them "
            "into USIS Construction Management. Sign in with Microsoft — the same "
            "SSO used on usiscm.com — then categorize files as drawings, specs, "
            "bid instructions, addenda, reports, schedules, or other."
        ),
    )
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)

    login = sub.add_parser("login", help="Sign in with Microsoft (device code)")
    login.add_argument("--access-token", help="Use an existing Microsoft access token instead of device login")

    sub.add_parser("logout", help="Forget the saved Microsoft session")
    sub.add_parser("whoami", help="Show the signed-in Microsoft / USISCM user")

    classify = sub.add_parser("classify", help="Classify a zip or folder without uploading")
    classify.add_argument("source", type=Path, help="Zip file or extracted package folder")
    classify.add_argument("--json", dest="json_out", type=Path, help="Write a classification manifest")
    classify.add_argument("--peek-pdf", action="store_true", help="Read first-page PDF text when PyMuPDF is installed")
    classify.add_argument("--work-dir", type=Path, help="Where to extract zips")

    import_cmd = sub.add_parser("import", help="Classify and upload one package to USISCM")
    import_cmd.add_argument("source", type=Path, help="Zip file or extracted package folder")
    import_cmd.add_argument("--project-id", help="USISCM project UUID (skips name matching)")
    import_cmd.add_argument("--dry-run", action="store_true", help="Resolve the project and classify only")
    import_cmd.add_argument("--peek-pdf", action="store_true")
    import_cmd.add_argument("--work-dir", type=Path)
    import_cmd.add_argument("--json", dest="json_out", type=Path)

    watch = sub.add_parser("watch", help="Poll a drop folder on this server and import new packages")
    watch.add_argument("directory", nargs="?", type=Path, help="Drop folder (default: USISCM_WATCH_DIR)")
    watch.add_argument("--project-id", help="Force every package onto one project UUID")
    watch.add_argument("--interval", type=int, help="Seconds between scans")
    watch.add_argument("--once", action="store_true", help="Process current packages and exit")
    watch.add_argument("--peek-pdf", action="store_true")
    watch.add_argument("--dry-run", action="store_true")

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
    if args.command == "classify":
        return _cmd_classify(args)
    if args.command == "import":
        return _cmd_import(args)
    if args.command == "watch":
        return _cmd_watch(args)
    parser.error(f"Unknown command {args.command}")
    return 2


def _cmd_login(args: argparse.Namespace) -> int:
    settings = load_settings()
    client = UsiscmClient(settings)
    try:
        app = client.entra_app()
        if args.access_token:
            save_tokens(settings.token_path, MicrosoftTokens(access_token=args.access_token, expires_at=time.time() + 3600))
            client.settings.ms_access_token = args.access_token
            client.login()
        else:
            print("Sign in with the same Microsoft account you use on usiscm.com.", flush=True)
            tokens = device_code_login(app)
            save_tokens(settings.token_path, tokens)
            client.token = tokens.access_token
            client.session.headers["Authorization"] = f"Bearer {tokens.access_token}"
        status = client.auth_status()
    except (MicrosoftAuthError, UsiscmError) as exc:
        logger.error("%s", exc)
        return 2
    user = (status.get("user") or {})
    print(f"Signed in as {user.get('email') or 'Microsoft user'} (saved {settings.token_path})")
    return 0 if status.get("authenticated") else 1


def _cmd_logout() -> int:
    settings = load_settings()
    clear_tokens(settings.token_path)
    print(f"Cleared Microsoft session at {settings.token_path}")
    return 0


def _cmd_whoami() -> int:
    try:
        status = UsiscmClient(load_settings()).auth_status()
    except UsiscmError as exc:
        logger.error("%s", exc)
        return 2
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
    drop = Path(drop).expanduser().resolve()
    drop.mkdir(parents=True, exist_ok=True)
    processed = settings.processed_dir or drop / "processed"
    failed = settings.failed_dir or drop / "failed"
    processed.mkdir(parents=True, exist_ok=True)
    failed.mkdir(parents=True, exist_ok=True)
    interval = args.interval or settings.poll_seconds
    client = None if args.dry_run else UsiscmClient(settings)

    def run_once() -> int:
        status = 0
        for package in iter_packages(drop):
            if package.name in {"processed", "failed"}:
                continue
            logger.info("Processing %s", package)
            manifest = ingest_source(package, work_dir=settings.work_dir, peek_pdf=args.peek_pdf)
            _print_manifest(manifest)
            dest_root = failed if manifest.errors else processed
            try:
                if not manifest.errors:
                    if client is None:
                        logger.info("Dry run — not uploading %s", package.name)
                    else:
                        result = client.import_package(
                            manifest,
                            project_id=args.project_id,
                            dry_run=False,
                        )
                        print(json.dumps(result.to_dict(), indent=2))
                        if result.errors:
                            dest_root = failed
                            status = 1
                dest = dest_root / stamp_name(package)
                if dest.exists():
                    dest = dest_root / f"{stamp_name(package)}_{id(package)}"
                shutil.move(str(package), str(dest))
                manifest.write_json(dest.with_suffix(dest.suffix + ".manifest.json") if dest.is_file() else dest / "manifest.json")
            except (UsiscmError, OSError) as exc:
                logger.error("Failed %s: %s", package, exc)
                status = 1
                try:
                    shutil.move(str(package), str(failed / stamp_name(package)))
                except OSError:
                    pass
        return status

    if args.once:
        return run_once()

    logger.info("Watching %s every %ss", drop, interval)
    while True:
        run_once()
        time.sleep(interval)


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
