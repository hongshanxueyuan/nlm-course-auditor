from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit import (
    StudioReader, audit_course, stopped_finding, write_report,
)
from scripts.errors import AuditAuthenticationError, AuditError, StudioReadError
from scripts.inputs import resolve_audit_input
from scripts.studio import StudioApiReader


def run_audit_request(
    *,
    working_directory: str | Path,
    tenant_config_path: str | Path = "tenant-config.json",
    tenant: str | None = None,
    course_id: str | None = None,
    studio_url: str | None = None,
    reader_factory: Callable[..., StudioReader] | None = None,
    environ: dict[str, str] | None = None,
    audited_at: str | None = None,
    output: TextIO | None = None,
) -> dict[str, Any]:
    resolved_tenant, resolved_course_id, studio_base, courses_base = resolve_audit_input(
        tenant=tenant,
        course_id=course_id,
        studio_url=studio_url,
        tenant_config_path=Path(tenant_config_path).expanduser().resolve(),
    )
    factory = reader_factory or StudioApiReader
    output_dir = Path(working_directory) / "auditor" / resolved_course_id.replace(":", "_")
    snapshot_path = output_dir / "course-structure.json"
    try:
        reader = factory(
            studio_base=studio_base,
            courses_base=courses_base,
            course_id=resolved_course_id,
            environ=environ,
        )
    except AuditAuthenticationError as exc:
        snapshot_path.unlink(missing_ok=True)
        finding = stopped_finding("authentication", "authentication_failure", exc.http_status)
        return write_report(
            tenant=resolved_tenant,
            course_id=resolved_course_id,
            audited_at=audited_at,
            target_sections=[],
            course_findings=[finding],
            output_dir=output_dir,
            output=output,
        )
    return audit_course(
        tenant=resolved_tenant,
        course_id=resolved_course_id,
        studio_reader=reader,
        working_directory=working_directory,
        audited_at=audited_at,
        output=output,
    )


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only structural audit of a saved Studio course")
    parser.add_argument("--tenant", help="configured tenant profile key")
    parser.add_argument("--course-id", help="complete course-v1:ORG+COURSE+RUN key")
    parser.add_argument("--studio-url", help="Studio course URL or vertical container URL")
    parser.add_argument("--tenant-config", default="tenant-config.json", help="tenant base URL configuration JSON")
    parser.add_argument("--working-directory", default=".", help="directory under which audit artifacts are saved")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        report = run_audit_request(
            tenant=args.tenant,
            course_id=args.course_id,
            studio_url=args.studio_url,
            tenant_config_path=Path(args.tenant_config).expanduser().resolve(),
            working_directory=Path(args.working_directory).expanduser().resolve(),
        )
        return 0 if report["status"] == "pass" else 1
    except AuditError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
