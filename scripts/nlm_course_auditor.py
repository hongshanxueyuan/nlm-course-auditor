from __future__ import annotations

import argparse
import copy
import io
import json
import sys
from pathlib import Path
from typing import Any, Callable, TextIO

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.audit import (
    StudioRepairClient,
    atomic_write_json,
    authenticated_studio_call,
    audit_course,
    http_status_of,
    normalize_unrecognized_bold_spacing,
    remove_supported_blockquote_prefixes,
    stopped_finding,
    subtitle_page_number,
    write_report,
)
from scripts.errors import AuditAuthenticationError, AuditError, StudioReadError
from scripts.inputs import (
    SAFE_BLOCK_LOCATOR_PATTERN,
    SEQUENTIAL_LOCATOR_PATTERN,
    VERTICAL_LOCATOR_PATTERN,
    resolve_audit_input,
)
from scripts.studio import StudioApiClient


def run_audit_request(
    *,
    working_directory: str | Path,
    tenant_config_path: str | Path = "tenant-config.json",
    tenant: str | None = None,
    course_id: str | None = None,
    studio_url: str | None = None,
    reader_factory: Callable[..., StudioRepairClient] | None = None,
    environ: dict[str, str] | None = None,
    audited_at: str | None = None,
    output: TextIO | None = None,
    audit_only: bool = False,
) -> dict[str, Any]:
    resolved_tenant, resolved_course_id, studio_base, courses_base = resolve_audit_input(
        tenant=tenant,
        course_id=course_id,
        studio_url=studio_url,
        tenant_config_path=Path(tenant_config_path).expanduser().resolve(),
    )
    factory = reader_factory or StudioApiClient
    output_dir = Path(working_directory) / "auditor" / resolved_course_id.replace(":", "_")
    snapshot_path = output_dir / "course-structure.json"
    try:
        studio_client = factory(
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
    initial_report = audit_course(
        tenant=resolved_tenant,
        course_id=resolved_course_id,
        studio_reader=studio_client,
        working_directory=working_directory,
        audited_at=audited_at,
        output=io.StringIO(),
    )
    candidates = [] if audit_only else _subtitle_repair_candidates(initial_report)
    publication_candidates = [] if audit_only else _sequential_publication_candidates(initial_report)
    repair_summary: dict[str, Any] = {
        "mode": "audit_only" if audit_only else "repair",
        "repair_passes": 1 if candidates or publication_candidates else 0,
        "targets": [],
    }
    final_report = initial_report
    if candidates:
        repair_summary["targets"] = _repair_subtitle_candidates(studio_client, candidates)
    if publication_candidates:
        repair_summary["sequential_publications"] = _publish_unpublished_sequential_sections(
            studio_client,
            publication_candidates,
        )
    if candidates or publication_candidates:
        final_report = audit_course(
            tenant=resolved_tenant,
            course_id=resolved_course_id,
            studio_reader=studio_client,
            working_directory=working_directory,
            audited_at=audited_at,
            output=io.StringIO(),
        )
    final_report["repair_summary"] = repair_summary
    atomic_write_json(output_dir / "report.json", final_report)
    print(json.dumps(final_report, ensure_ascii=False, indent=2), file=output or sys.stdout)
    return final_report


def _sequential_publication_candidates(report: dict[str, Any]) -> list[dict[str, str]]:
    candidates: list[dict[str, str]] = []
    seen_locators: set[str] = set()
    for section in report.get("sequential_sections", []):
        locator = section.get("locator") if isinstance(section, dict) else None
        if not isinstance(locator, str) or not SEQUENTIAL_LOCATOR_PATTERN.fullmatch(locator):
            continue
        has_publication_issue = any(
            check.get("check_id") == "NLMCA-013" and check.get("status") == "issue"
            for check in section.get("checks", [])
            if isinstance(check, dict)
        )
        if has_publication_issue and locator not in seen_locators:
            candidates.append({"sequential_locator": locator})
            seen_locators.add(locator)
    return candidates


def _publish_unpublished_sequential_sections(
    studio_client: StudioRepairClient,
    candidates: list[dict[str, str]],
) -> list[dict[str, Any]]:
    publications: list[dict[str, Any]] = []
    for candidate in candidates:
        sequential_locator = candidate["sequential_locator"]
        publication: dict[str, Any] = {
            "sequential_locator": sequential_locator,
            "publish_status": "failed",
        }
        try:
            response = authenticated_studio_call(
                studio_client,
                "publish_sequential",
                lambda: studio_client.publish_sequential(sequential_locator),
            )
        except Exception as exc:
            if http_status_of(exc) is not None:
                publication["publish_http_status"] = http_status_of(exc)
        else:
            publication["publish_status"] = "completed" if _write_response_is_valid(response) else "failed"
        publications.append(publication)
    return publications


def _subtitle_repair_candidates(report: dict[str, Any]) -> list[dict[str, Any]]:
    candidates: dict[tuple[str, str], dict[str, Any]] = {}
    for section in report.get("target_sections", []):
        vertical_locator = section.get("locator")
        if not isinstance(vertical_locator, str) or not VERTICAL_LOCATOR_PATTERN.fullmatch(vertical_locator):
            continue
        for check in section.get("checks", []):
            check_id = check.get("check_id")
            if check_id not in {"NLMCA-010", "NLMCA-012"} or check.get("status") != "issue":
                continue
            evidence = check.get("evidence")
            gallery_locator = evidence.get("gallery_locator") if isinstance(evidence, dict) else None
            if not isinstance(gallery_locator, str) or not SAFE_BLOCK_LOCATOR_PATTERN.fullmatch(gallery_locator):
                continue
            identity = (vertical_locator, gallery_locator)
            candidate = candidates.setdefault(identity, {
                "vertical_locator": vertical_locator,
                "gallery_locator": gallery_locator,
                "repair_types": [],
                "affected_pages": [],
                "marker_count": 0,
                "bold_affected_pages": [],
                "unrecognized_bold_count": 0,
            })
            if check_id == "NLMCA-010":
                pages = evidence.get("affected_pages", [])
                candidate["affected_pages"] = [
                    {"page": item["page"], "marker_count": item["marker_count"]}
                    for item in pages
                    if isinstance(item, dict)
                    and isinstance(item.get("page"), int)
                    and not isinstance(item.get("page"), bool)
                    and isinstance(item.get("marker_count"), int)
                    and not isinstance(item.get("marker_count"), bool)
                ] if isinstance(pages, list) else []
                marker_count = evidence.get("total_marker_count")
                candidate["marker_count"] = marker_count if isinstance(marker_count, int) and not isinstance(marker_count, bool) else 0
                candidate["repair_types"].append("blockquote")
            else:
                pages = evidence.get("affected_pages", [])
                candidate["bold_affected_pages"] = [
                    {"page": item["page"], "unrecognized_bold_count": item["unrecognized_bold_count"]}
                    for item in pages
                    if isinstance(item, dict)
                    and isinstance(item.get("page"), int)
                    and not isinstance(item.get("page"), bool)
                    and isinstance(item.get("unrecognized_bold_count"), int)
                    and not isinstance(item.get("unrecognized_bold_count"), bool)
                ] if isinstance(pages, list) else []
                bold_count = evidence.get("unrecognized_bold_count")
                candidate["unrecognized_bold_count"] = bold_count if isinstance(bold_count, int) and not isinstance(bold_count, bool) else 0
                candidate["repair_types"].append("bold")
    return list(candidates.values())


def _complete_play_config(payload: Any) -> dict[str, Any] | None:
    play_config = payload.get("play_config") if isinstance(payload, dict) else None
    time_config = play_config.get("time_config") if isinstance(play_config, dict) else None
    if (
        not isinstance(play_config, dict)
        or (
            "show_subtitles" in play_config
            and not isinstance(play_config.get("show_subtitles"), bool)
        )
        or not isinstance(time_config, list)
        or not time_config
    ):
        return None
    if any(
        not isinstance(item, dict)
        or not isinstance(item.get("page"), int)
        or isinstance(item.get("page"), bool)
        or item["page"] <= 0
        or not isinstance(item.get("start"), int)
        or isinstance(item.get("start"), bool)
        or item["start"] < 0
        or not isinstance(item.get("end"), int)
        or isinstance(item.get("end"), bool)
        or item["end"] < item["start"]
        or not isinstance(item.get("subtitle"), str)
        or not item["subtitle"].strip()
        for item in time_config
    ):
        return None
    return copy.deepcopy(play_config)


def _write_response_is_valid(payload: Any) -> bool:
    return (
        isinstance(payload, dict)
        and payload.get("success") is not False
        and not any(key in payload for key in ("error", "errors", "detail"))
    )


def _repair_one_gallery(studio_client: StudioRepairClient, candidate: dict[str, Any]) -> dict[str, Any]:
    bold_attempted = "bold" in candidate["repair_types"]
    result = {
        "gallery_locator": candidate["gallery_locator"],
        "repair_status": "skipped",
        "reason": "play_config_unverified",
        "affected_pages": candidate["affected_pages"],
        "marker_count": candidate["marker_count"],
        "bold_repair_status": "skipped" if bold_attempted else "not_required",
        "bold_affected_pages": [
            {**item, "status": "skipped", "reason": "play_config_unverified"}
            for item in candidate["bold_affected_pages"]
        ],
        "unrecognized_bold_count": candidate["unrecognized_bold_count"],
    }
    try:
        payload = authenticated_studio_call(
            studio_client,
            "get_play_config",
            lambda: studio_client.get_play_config(candidate["gallery_locator"]),
        )
    except Exception as exc:
        if http_status_of(exc) is not None:
            result["http_status"] = http_status_of(exc)
        return result
    play_config = _complete_play_config(payload)
    if play_config is None:
        return result

    page_counts: dict[int, int] = {}
    bold_page_counts = {
        item["page"]: item["unrecognized_bold_count"]
        for item in candidate["bold_affected_pages"]
    }
    bold_outcomes: list[dict[str, Any]] = []
    changed = False
    for index, item in enumerate(play_config["time_config"], start=1):
        subtitle = item["subtitle"]
        page_number = subtitle_page_number(item, index)
        updated_subtitle = subtitle
        marker_count = 0
        if "blockquote" in candidate["repair_types"]:
            updated_subtitle, marker_count = remove_supported_blockquote_prefixes(updated_subtitle)
        if page_number in bold_page_counts:
            bold_subtitle, reason = normalize_unrecognized_bold_spacing(updated_subtitle)
            if bold_subtitle is None:
                bold_status = "skipped"
            elif reason == "already_recognized" and updated_subtitle == subtitle:
                bold_status = "skipped"
                reason = "no_longer_present"
            else:
                bold_status = "repaired"
                if reason == "already_recognized":
                    reason = "resolved_by_other_approved_repair"
            bold_outcomes.append({
                "page": page_number,
                "unrecognized_bold_count": bold_page_counts[page_number],
                "status": bold_status,
                "reason": reason,
            })
            if bold_subtitle is not None:
                updated_subtitle = bold_subtitle
        if updated_subtitle != subtitle:
            item["subtitle"] = updated_subtitle
            if marker_count:
                page_counts[page_number] = page_counts.get(page_number, 0) + marker_count
            changed = True

    if bold_attempted:
        result["bold_affected_pages"] = bold_outcomes
        repaired_bold_pages = sum(item["status"] == "repaired" for item in bold_outcomes)
        skipped_bold_pages = sum(item["status"] == "skipped" for item in bold_outcomes)
        if repaired_bold_pages and skipped_bold_pages:
            result["bold_repair_status"] = "partial"
        elif repaired_bold_pages:
            result["bold_repair_status"] = "repaired"
        else:
            result["bold_repair_status"] = "skipped"
    if not changed:
        result["reason"] = "no_safe_subtitle_change" if bold_attempted else "no_supported_prefix_remained"
        return result

    result["affected_pages"] = [
        {"page": page, "marker_count": count}
        for page, count in sorted(page_counts.items())
    ]
    result["marker_count"] = sum(page_counts.values())
    try:
        response = authenticated_studio_call(
            studio_client,
            "update_play_config",
            lambda: studio_client.update_play_config(candidate["gallery_locator"], play_config),
        )
    except Exception as exc:
        result.update({"repair_status": "failed", "reason": "update_failed"})
        if bold_attempted:
            result["bold_repair_status"] = "failed"
            for item in result["bold_affected_pages"]:
                if item["status"] == "repaired":
                    item["status"] = "failed"
                    item["reason"] = "update_failed"
        if http_status_of(exc) is not None:
            result["http_status"] = http_status_of(exc)
        return result
    if not _write_response_is_valid(response):
        result.update({"repair_status": "failed", "reason": "update_response_unverified"})
        if bold_attempted:
            result["bold_repair_status"] = "failed"
            for item in result["bold_affected_pages"]:
                if item["status"] == "repaired":
                    item["status"] = "failed"
                    item["reason"] = "update_response_unverified"
        return result
    result.update({"repair_status": "repaired", "reason": "completed"})
    return result


def _repair_subtitle_candidates(
    studio_client: StudioRepairClient,
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    targets_by_vertical: dict[str, dict[str, Any]] = {}
    changed_verticals: set[str] = set()
    for candidate in candidates:
        vertical_locator = candidate["vertical_locator"]
        target = targets_by_vertical.setdefault(vertical_locator, {
            "vertical_locator": vertical_locator,
            "publish_status": "not_required",
            "galleries": [],
        })
        result = _repair_one_gallery(studio_client, candidate)
        target["galleries"].append(result)
        if result["repair_status"] == "repaired":
            changed_verticals.add(vertical_locator)

    for vertical_locator, target in targets_by_vertical.items():
        if vertical_locator not in changed_verticals:
            continue
        try:
            response = authenticated_studio_call(
                studio_client,
                "publish_vertical",
                lambda: studio_client.publish_vertical(vertical_locator),
            )
        except Exception as exc:
            target["publish_status"] = "failed"
            if http_status_of(exc) is not None:
                target["publish_http_status"] = http_status_of(exc)
        else:
            target["publish_status"] = "completed" if _write_response_is_valid(response) else "failed"
    return list(targets_by_vertical.values())


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Audit a saved Studio course, repair approved subtitle issues, and publish confirmed unpublished subsections"
    )
    parser.add_argument("--tenant", help="configured tenant profile key")
    parser.add_argument("--course-id", help="complete course-v1:ORG+COURSE+RUN key")
    parser.add_argument("--studio-url", help="Studio course URL or vertical container URL")
    parser.add_argument("--tenant-config", default="tenant-config.json", help="tenant base URL configuration JSON")
    parser.add_argument("--working-directory", default=".", help="directory under which audit artifacts are saved")
    parser.add_argument("--audit-only", action="store_true", help="report findings without changing or publishing Studio content")
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
            audit_only=args.audit_only,
        )
        return 0 if report["status"] == "pass" else 1
    except AuditError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
