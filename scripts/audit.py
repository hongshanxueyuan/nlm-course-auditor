from __future__ import annotations

import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from .errors import AuditError, GlobalAuthenticationError
from .inputs import SAFE_BLOCK_LOCATOR_PATTERN, VERTICAL_LOCATOR_PATTERN, validate_course_key


MAX_AUTH_REFRESHES = 4


TARGET_VERTICAL_NAME = "赋能内容"


SUMMARY_COMPONENT_NAME = "本节要点"


class StudioReader(Protocol):
    studio_base: str

    def get_course_structure(self, course_id: str) -> Any: ...

    def get_files(self, gallery_locator: str) -> Any: ...

    def get_play_config(self, gallery_locator: str) -> Any: ...

    def get_audios(self, gallery_locator: str) -> Any: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def atomic_write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(payload, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_name, path)
    finally:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass


def children_of(node: Any) -> list[dict[str, Any]]:
    if not isinstance(node, dict):
        return []
    child_info = node.get("child_info")
    children = child_info.get("children") if isinstance(child_info, dict) else None
    return [child for child in children if isinstance(child, dict)] if isinstance(children, list) else []


def category_of(node: dict[str, Any]) -> str:
    return str(node.get("category", "")).strip().lower()


def locator_of(node: dict[str, Any]) -> str:
    for key in ("id", "block_location", "locator"):
        value = node.get(key)
        if isinstance(value, str) and SAFE_BLOCK_LOCATOR_PATTERN.fullmatch(value.strip()):
            return value.strip()
    return ""


def vertical_locator_of(node: dict[str, Any]) -> str:
    locator = locator_of(node)
    return locator if VERTICAL_LOCATOR_PATTERN.fullmatch(locator) else ""


def vertical_studio_url(studio_base: str, vertical_locator: str) -> str | None:
    if not VERTICAL_LOCATOR_PATTERN.fullmatch(vertical_locator):
        return None
    return f"{studio_base.rstrip('/')}/container/{vertical_locator}"


def display_name_of(node: dict[str, Any]) -> str:
    value = node.get("display_name")
    return value if isinstance(value, str) else ""


def child_position_record(node: dict[str, Any], index: int) -> dict[str, Any]:
    return {
        "position": index + 1,
        "category": category_of(node),
        "name": display_name_of(node).strip(),
    }


def component_positions(
    children: list[dict[str, Any]], *, category: str, name: str | None = None,
) -> list[int]:
    return [
        index + 1
        for index, child in enumerate(children)
        if category_of(child) == category
        and (name is None or display_name_of(child).strip() == name)
    ]


def find_target_verticals(course_tree: Any) -> list[dict[str, Any]]:
    if not isinstance(course_tree, dict):
        raise AuditError("Studio returned an invalid course-structure response.")
    targets: list[dict[str, Any]] = []

    def visit(node: dict[str, Any]) -> None:
        if category_of(node) == "vertical" and display_name_of(node).strip() == TARGET_VERTICAL_NAME:
            targets.append(node)
        for child in children_of(node):
            visit(child)

    visit(course_tree)
    return targets


def course_tree_is_complete(course_tree: Any) -> bool:
    if not isinstance(course_tree, dict):
        return False
    root_info = course_tree.get("child_info")
    if not isinstance(root_info, dict) or not isinstance(root_info.get("children"), list):
        return False

    container_categories = {"course", "chapter", "sequential", "vertical"}

    def valid_node(node: Any) -> bool:
        if not isinstance(node, dict):
            return False
        child_info = node.get("child_info")
        if child_info is None and "child_info" not in node:
            return category_of(node) not in container_categories
        if child_info is None and category_of(node) not in container_categories:
            return True
        if not isinstance(child_info, dict) or not isinstance(child_info.get("children"), list):
            return False
        return all(valid_node(child) for child in child_info["children"])

    return all(valid_node(child) for child in root_info["children"])


def check_result(
    check_id: str,
    category: str,
    name: str,
    status: str,
    expected: Any,
    actual: Any,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    return {
        "check_id": check_id,
        "category": category,
        "name": name,
        "status": status,
        "expected": expected,
        "actual": actual,
        "evidence": evidence,
    }


def issue_or_pass(actual_count: int, expected_count: int) -> str:
    return "pass" if actual_count == expected_count else "issue"


def http_status_of(error: BaseException) -> int | None:
    status = getattr(error, "http_status", None)
    return status if isinstance(status, int) and not isinstance(status, bool) else None


def safe_file_records(payload: Any) -> tuple[list[dict[str, Any]] | None, int | None]:
    if not isinstance(payload, dict) or not isinstance(payload.get("files"), list):
        return None, None
    files = payload["files"]
    total_count = payload.get("total_count")
    if not isinstance(total_count, int) or isinstance(total_count, bool) or total_count < 0:
        return None, None
    ids: set[str] = set()
    for item in files:
        if not isinstance(item, dict):
            return None, None
        asset_id = item.get("id") or item.get("asset_key")
        if not isinstance(asset_id, str) or not asset_id.strip():
            return None, None
        asset_id = asset_id.strip()
        if asset_id in ids:
            return None, None
        ids.add(asset_id)
    return files, total_count


def read_failure(stage: str, exc: BaseException) -> dict[str, Any]:
    http_status = http_status_of(exc)
    summary = (
        f"Studio {stage} read failed with HTTP {http_status}."
        if isinstance(http_status, int)
        else f"Studio {stage} read did not produce a verifiable response."
    )
    evidence: dict[str, Any] = {
        "failure_stage": stage,
        "failure_category": "read_failure",
        "summary": summary,
    }
    if http_status is not None:
        evidence["http_status"] = http_status
    return evidence


def authenticated_read(studio_reader: StudioReader, stage: str, operation: Any) -> Any:
    refresh_count = 0
    while True:
        try:
            return operation()
        except Exception as exc:
            http_status = http_status_of(exc)
            is_auth_failure = bool(getattr(exc, "auth_failure", False)) or http_status in (401, 403)
            if not is_auth_failure:
                raise
            refresh = getattr(studio_reader, "refresh_authentication", None)
            if not callable(refresh) or refresh_count >= MAX_AUTH_REFRESHES:
                raise GlobalAuthenticationError(stage, http_status) from exc
            refresh_count += 1
            try:
                refresh()
            except Exception as refresh_error:
                refresh_status = http_status_of(refresh_error) or http_status
                raise GlobalAuthenticationError(stage, refresh_status) from refresh_error


def gallery_checks(studio_reader: StudioReader, gallery_locator: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    files: list[dict[str, Any]] | None = None
    total_count: int | None = None
    file_failure: dict[str, Any] | None = None
    try:
        payload = authenticated_read(
            studio_reader,
            "get_files",
            lambda: studio_reader.get_files(gallery_locator),
        )
        files, total_count = safe_file_records(payload)
        if files is None:
            file_failure = {
                "failure_stage": "get_files",
                "failure_category": "invalid_response",
                "summary": "Studio get_files did not return a complete list of uniquely identified file records.",
            }
    except GlobalAuthenticationError as exc:
        exc.completed_checks = list(checks)
        raise
    except Exception as exc:
        file_failure = read_failure("get_files", exc)

    if file_failure:
        checks.append(check_result(
            "NLMCA-003", "ImageGallery", "Gallery file count matches total_count", "unverified",
            "Valid file-record count equals Studio total_count", None,
            {"gallery_locator": gallery_locator, **file_failure},
        ))
    else:
        assert files is not None and total_count is not None
        file_count = len(files)
        checks.append(check_result(
            "NLMCA-003", "ImageGallery", "Gallery file count matches total_count",
            "pass" if file_count == total_count else "issue",
            {"file_count_equals_total_count": True},
            {"valid_file_count": file_count, "total_count": total_count},
            {"gallery_locator": gallery_locator, "valid_file_count": file_count, "total_count": total_count},
        ))

    try:
        config_payload = authenticated_read(
            studio_reader,
            "get_play_config",
            lambda: studio_reader.get_play_config(gallery_locator),
        )
        play_config = config_payload.get("play_config") if isinstance(config_payload, dict) else None
        time_config = play_config.get("time_config") if isinstance(play_config, dict) else None
        if not isinstance(time_config, list):
            config_failure = {
                "failure_stage": "get_play_config",
                "failure_category": "invalid_response",
                "summary": "Studio play_config.time_config is not a list.",
            }
        else:
            config_failure = None
    except GlobalAuthenticationError as exc:
        exc.completed_checks = list(checks)
        raise
    except Exception as exc:
        time_config = None
        config_failure = read_failure("get_play_config", exc)

    if config_failure:
        checks.append(check_result(
            "NLMCA-004", "ImageGallery", "Timing-entry count matches valid files", "unverified",
            {"time_config_count_equals_valid_file_count": True}, None,
            {"gallery_locator": gallery_locator, **config_failure},
        ))
    elif files is None:
        checks.append(check_result(
            "NLMCA-004", "ImageGallery", "Timing-entry count matches valid files", "unverified",
            {"time_config_count_equals_valid_file_count": True}, None,
            {
                "gallery_locator": gallery_locator,
                "failure_stage": "get_files",
                "failure_category": "invalid_response",
                "summary": "A valid file-record count was unavailable for comparison.",
            },
        ))
    else:
        assert isinstance(time_config, list)
        config_count = len(time_config)
        file_count = len(files)
        checks.append(check_result(
            "NLMCA-004", "ImageGallery", "Timing-entry count matches valid files",
            "pass" if config_count == file_count else "issue",
            {"time_config_count_equals_valid_file_count": True},
            {"time_config_count": config_count, "valid_file_count": file_count},
            {"gallery_locator": gallery_locator, "time_config_count": config_count, "valid_file_count": file_count},
        ))

    try:
        audio_payload = authenticated_read(
            studio_reader,
            "get_audios",
            lambda: studio_reader.get_audios(gallery_locator),
        )
        audios = audio_payload.get("audios") if isinstance(audio_payload, dict) else None
        if not isinstance(audios, list) or any(not isinstance(item, dict) for item in audios):
            audio_failure = {
                "failure_stage": "get_audios",
                "failure_category": "invalid_response",
                "summary": "Studio get_audios.audios is not a list of bindings.",
            }
        else:
            audio_failure = None
    except GlobalAuthenticationError as exc:
        exc.completed_checks = list(checks)
        raise
    except Exception as exc:
        audios = None
        audio_failure = read_failure("get_audios", exc)

    if audio_failure:
        checks.append(check_result(
            "NLMCA-005", "ImageGallery", "Gallery has exactly one audio binding", "unverified",
            {"audio_binding_count": 1}, None,
            {"gallery_locator": gallery_locator, **audio_failure},
        ))
    else:
        assert isinstance(audios, list)
        audio_count = len(audios)
        checks.append(check_result(
            "NLMCA-005", "ImageGallery", "Gallery has exactly one audio binding",
            "pass" if audio_count == 1 else "issue",
            {"audio_binding_count": 1}, {"audio_binding_count": audio_count},
            {"gallery_locator": gallery_locator, "audio_binding_count": audio_count},
        ))
    return checks


def section_checks(studio_reader: StudioReader, vertical: dict[str, Any]) -> list[dict[str, Any]]:
    vertical_locator = vertical_locator_of(vertical)
    if not vertical_locator:
        summary = "Studio course structure did not identify a valid vertical locator."
        return [
            check_result(
                check_id,
                category,
                name,
                "unverified",
                expected,
                None,
                {
                    "failure_stage": "course_structure_lookup",
                    "failure_category": "invalid_vertical_locator",
                    "summary": summary,
                },
            )
            for check_id, category, name, expected in (
                ("NLMCA-001", "组件结构", "目标小节恰有一个 ImageGallery", {"imagesgallery_count": 1}),
                ("NLMCA-002", "组件结构", "目标小节恰有一个“本节要点” HTML 组件", {"matching_html_count": 1}),
                ("NLMCA-003", "ImageGallery", "Gallery file count matches total_count", "Valid file-record count equals Studio total_count"),
                ("NLMCA-004", "ImageGallery", "Timing-entry count matches valid files", {"time_config_count_equals_valid_file_count": True}),
                ("NLMCA-005", "ImageGallery", "Gallery has exactly one audio binding", {"audio_binding_count": 1}),
                ("NLMCA-008", "组件顺序", "ImageGallery 位于目标小节首位", {"first_child_category": "imagesgallery"}),
                ("NLMCA-009", "组件顺序", "“本节要点”位于目标小节末尾", {"last_child_category": "html", "last_child_name": SUMMARY_COMPONENT_NAME}),
            )
        ]
    children = children_of(vertical)
    galleries = [child for child in children if category_of(child) == "imagesgallery"]
    named_html = [
        child for child in children
        if category_of(child) == "html" and display_name_of(child).strip() == SUMMARY_COMPONENT_NAME
    ]

    checks = [
        check_result(
            "NLMCA-001", "组件结构", "目标小节恰有一个 ImageGallery",
            issue_or_pass(len(galleries), 1), {"imagesgallery_count": 1}, {"count": len(galleries)},
            {"vertical_locator": vertical_locator, "component_category": "imagesgallery"},
        ),
        check_result(
            "NLMCA-002", "组件结构", "目标小节恰有一个“本节要点” HTML 组件",
            issue_or_pass(len(named_html), 1), {"matching_html_count": 1}, {"count": len(named_html)},
            {"vertical_locator": vertical_locator, "component_category": "html", "name": SUMMARY_COMPONENT_NAME},
        ),
    ]
    first_child = child_position_record(children[0], 0) if children else None
    last_child = child_position_record(children[-1], len(children) - 1) if children else None
    gallery_positions = component_positions(children, category="imagesgallery")
    summary_positions = component_positions(children, category="html", name=SUMMARY_COMPONENT_NAME)
    order_checks = [
        check_result(
            "NLMCA-008", "组件顺序", "ImageGallery 位于目标小节首位",
            "pass" if first_child is not None and first_child["category"] == "imagesgallery" else "issue",
            {"first_child_category": "imagesgallery"},
            {
                "direct_child_count": len(children),
                "imagesgallery_positions": gallery_positions,
                "first_child": first_child,
            },
            {
                "vertical_locator": vertical_locator,
                "direct_child_count": len(children),
                "imagesgallery_positions": gallery_positions,
                "first_child": first_child,
            },
        ),
        check_result(
            "NLMCA-009", "组件顺序", "“本节要点”位于目标小节末尾",
            "pass" if last_child is not None and last_child["category"] == "html" and last_child["name"] == SUMMARY_COMPONENT_NAME else "issue",
            {"last_child_category": "html", "last_child_name": SUMMARY_COMPONENT_NAME},
            {
                "direct_child_count": len(children),
                "summary_positions": summary_positions,
                "last_child": last_child,
            },
            {
                "vertical_locator": vertical_locator,
                "direct_child_count": len(children),
                "summary_positions": summary_positions,
                "last_child": last_child,
            },
        ),
    ]
    gallery_data_checks: list[dict[str, Any]] = []
    for gallery in galleries:
        gallery_locator = locator_of(gallery)
        if not gallery_locator:
            gallery_data_checks.extend([
                check_result(
                    check_id, "ImageGallery", name, "unverified", expected, None,
                    {
                        "vertical_locator": vertical_locator,
                        "failure_stage": "course_structure_lookup",
                        "failure_category": "invalid_response",
                        "summary": "Studio course structure did not identify the Gallery locator.",
                    },
                )
                for check_id, name, expected in (
                    ("NLMCA-003", "Gallery file count matches total_count", "Valid file-record count equals Studio total_count"),
                    ("NLMCA-004", "Timing-entry count matches valid files", {"time_config_count_equals_valid_file_count": True}),
                    ("NLMCA-005", "Gallery has exactly one audio binding", {"audio_binding_count": 1}),
                )
            ])
            continue
        try:
            gallery_data_checks.extend(gallery_checks(studio_reader, gallery_locator))
        except GlobalAuthenticationError as exc:
            exc.completed_checks = (
                checks
                + gallery_data_checks
                + getattr(exc, "completed_checks", [])
                + order_checks
            )
            raise
    return checks + gallery_data_checks + order_checks


def aggregate_status(checks: list[dict[str, Any]], course_findings: list[dict[str, Any]]) -> tuple[str, int, int, int]:
    statuses = [check["status"] for check in checks]
    statuses.extend(finding["status"] for finding in course_findings)
    issue_count = statuses.count("issue")
    unverified_count = statuses.count("unverified")
    pass_count = statuses.count("pass")
    status = "issue" if issue_count else "unverified" if unverified_count else "pass"
    return status, pass_count, issue_count, unverified_count


def make_report(
    *,
    tenant: str,
    course_id: str,
    audited_at: str | None,
    target_sections: list[dict[str, Any]],
    course_findings: list[dict[str, Any]],
) -> dict[str, Any]:
    checks = [check for section in target_sections for check in section["checks"]]
    status, pass_count, issue_count, unverified_count = aggregate_status(checks, course_findings)
    return {
        "schema_version": "1.0.0",
        "tenant": tenant,
        "course_id": course_id,
        "audited_at": audited_at or utc_now(),
        "status": status,
        "pass_count": pass_count,
        "issue_count": issue_count,
        "unverified_count": unverified_count,
        "course_findings": course_findings,
        "target_sections": target_sections,
    }


def write_report(
    *,
    tenant: str,
    course_id: str,
    audited_at: str | None,
    target_sections: list[dict[str, Any]],
    course_findings: list[dict[str, Any]],
    output_dir: Path,
    output: TextIO | None,
) -> dict[str, Any]:
    report = make_report(
        tenant=tenant,
        course_id=course_id,
        audited_at=audited_at,
        target_sections=target_sections,
        course_findings=course_findings,
    )
    atomic_write_json(output_dir / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=output or sys.stdout)
    return report


def stopped_finding(stage: str, category: str, http_status: int | None = None) -> dict[str, Any]:
    if not isinstance(http_status, int) or isinstance(http_status, bool):
        http_status = None
    summary = (
        f"Studio {stage} read failed with HTTP {http_status}."
        if isinstance(http_status, int)
        else f"Studio {stage} could not be verified."
    )
    evidence: dict[str, Any] = {
        "failure_stage": stage,
        "failure_category": category,
        "summary": summary,
    }
    if isinstance(http_status, int):
        evidence["http_status"] = http_status
    return {
        "check_id": "NLMCA-007",
        "category": "课程读取",
        "name": "课程审核读取完成",
        "status": "unverified",
        "expected": {"audit_reads": "complete"},
        "actual": {"audit_reads": "stopped"},
        "evidence": evidence,
    }


def audit_course(
    *,
    tenant: str,
    course_id: str,
    studio_reader: StudioReader,
    working_directory: str | Path,
    audited_at: str | None = None,
    output: TextIO | None = None,
) -> dict[str, Any]:
    """Run one complete audit through an authenticated Studio reader."""
    if not isinstance(tenant, str) or not tenant.strip():
        raise AuditError("Provide a configured tenant profile name.")
    validate_course_key(course_id)
    safe_course_id = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", course_id).rstrip(" .")
    output_dir = Path(working_directory) / "auditor" / safe_course_id
    snapshot_path = output_dir / "course-structure.json"
    snapshot_path.unlink(missing_ok=True)
    try:
        course_tree = authenticated_read(
            studio_reader,
            "course_structure",
            lambda: studio_reader.get_course_structure(course_id),
        )
    except GlobalAuthenticationError as exc:
        finding = stopped_finding("authentication", "authentication_failure", exc.http_status)
        return write_report(
            tenant=tenant,
            course_id=course_id,
            audited_at=audited_at,
            target_sections=[],
            course_findings=[finding],
            output_dir=output_dir,
            output=output,
        )
    except Exception as exc:
        finding = stopped_finding("course_structure", "read_failure", http_status_of(exc))
        return write_report(
            tenant=tenant,
            course_id=course_id,
            audited_at=audited_at,
            target_sections=[],
            course_findings=[finding],
            output_dir=output_dir,
            output=output,
        )
    if not course_tree_is_complete(course_tree):
        finding = stopped_finding("course_structure", "invalid_response")
        return write_report(
            tenant=tenant,
            course_id=course_id,
            audited_at=audited_at,
            target_sections=[],
            course_findings=[finding],
            output_dir=output_dir,
            output=output,
        )
    atomic_write_json(output_dir / "course-structure.json", course_tree)

    target_sections: list[dict[str, Any]] = []
    for vertical in find_target_verticals(course_tree):
        try:
            checks = section_checks(studio_reader, vertical)
        except GlobalAuthenticationError as exc:
            partial_checks = getattr(exc, "completed_checks", [])
            if partial_checks:
                vertical_locator = vertical_locator_of(vertical)
                target_sections.append({
                    "name": display_name_of(vertical),
                    "locator": vertical_locator,
                    "studio_url": vertical_studio_url(studio_reader.studio_base, vertical_locator),
                    "checks": partial_checks,
                })
            course_findings = [stopped_finding("authentication", "authentication_failure", exc.http_status)]
            return write_report(
                tenant=tenant,
                course_id=course_id,
                audited_at=audited_at,
                target_sections=target_sections,
                course_findings=course_findings,
                output_dir=output_dir,
                output=output,
            )
        section = {
            "name": display_name_of(vertical),
            "locator": vertical_locator_of(vertical),
            "studio_url": vertical_studio_url(studio_reader.studio_base, vertical_locator_of(vertical)),
            "checks": checks,
        }
        target_sections.append(section)

    course_findings: list[dict[str, Any]] = []
    if not target_sections:
        course_findings.append({
            "check_id": "NLMCA-006",
            "category": "课程范围",
            "name": "课程结构中找到赋能内容目标小节",
            "status": "issue",
            "expected": {"target_section_count": "at least 1"},
            "actual": {"target_section_count": 0},
            "evidence": {
                "failure_stage": "target_discovery",
                "failure_category": "empty_target_set",
                "summary": "No matching target vertical was found; confirm the tenant, course, and Studio naming.",
            },
        })

    return write_report(
        tenant=tenant,
        course_id=course_id,
        audited_at=audited_at,
        target_sections=target_sections,
        course_findings=course_findings,
        output_dir=output_dir,
        output=output,
    )
