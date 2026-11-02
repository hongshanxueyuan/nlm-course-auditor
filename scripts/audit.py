from __future__ import annotations

import html
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO

from markdown_it import MarkdownIt

from .errors import AuditError, GlobalAuthenticationError
from .inputs import SAFE_BLOCK_LOCATOR_PATTERN, VERTICAL_LOCATOR_PATTERN, validate_course_key


MAX_AUTH_REFRESHES = 4


TARGET_VERTICAL_NAME = "赋能内容"


SUMMARY_COMPONENT_NAME = "本节要点"
TIME_CONFIG_EXCEEDS_FILES_HINT = "大概率是 imagegallery 有几页图片未成功上传。"
INVALID_TIME_CONFIG_HINT = "大概率是 imagegallery 的音频字幕未成功上传。"
BLOCKQUOTE_PREFIX_RE = re.compile(r"^ {0,3}(?:>+ *)+", re.MULTILINE)
SAVED_SUBTITLE_CHECK_NAME = "Saved Gallery subtitle text has no blockquote prefix risk"
SUBTITLE_HEADING_ORDER_CHECK_NAME = "Readable Gallery subtitles begin with a paragraph before any Markdown heading"
SUBTITLE_BOLD_CHECK_NAME = "Saved Gallery subtitles use CommonMark-recognized bold delimiters"
SUBTITLE_HEADING_ORDER_HINT = "大概率是 AI 段落划分不准确。"
SUBTITLE_HEADING_ORDER_EXPECTED = {"first_content_block": "paragraph", "later_markdown_heading": True}
SUBTITLE_MARKDOWN = MarkdownIt("commonmark").enable("table")


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
    *,
    operator_hint: str | None = None,
) -> dict[str, Any]:
    result = {
        "check_id": check_id,
        "category": category,
        "name": name,
        "status": status,
        "expected": expected,
        "actual": actual,
        "evidence": evidence,
    }
    if operator_hint is not None:
        result["operator_hint"] = operator_hint
    return result


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


def gallery_checks(
    studio_reader: StudioReader, gallery_locator: str, section_studio_url: str | None,
) -> list[dict[str, Any]]:
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

    invalid_time_config = False
    try:
        config_payload = authenticated_read(
            studio_reader,
            "get_play_config",
            lambda: studio_reader.get_play_config(gallery_locator),
        )
        play_config = config_payload.get("play_config") if isinstance(config_payload, dict) else None
        time_config = play_config.get("time_config") if isinstance(play_config, dict) else None
        if not isinstance(time_config, list):
            invalid_time_config = True
            config_failure = {
                "failure_stage": "get_play_config",
                "failure_category": "invalid_response",
                "summary": "Studio play_config.time_config is not a list.",
            }
        else:
            config_failure = None
    except GlobalAuthenticationError as exc:
        config_failure = read_failure("get_play_config", exc)
        subtitle_check = saved_subtitle_check(
            None,
            gallery_locator,
            section_studio_url=section_studio_url,
            config_failure=config_failure,
        )
        heading_order_check = subtitle_heading_order_check(
            None,
            gallery_locator,
            section_studio_url=section_studio_url,
        )
        bold_check = subtitle_bold_check(None, gallery_locator, section_studio_url=section_studio_url)
        exc.completed_checks = list(checks) + [subtitle_check, heading_order_check, bold_check]
        raise
    except Exception as exc:
        time_config = None
        config_failure = read_failure("get_play_config", exc)

    if config_failure:
        checks.append(check_result(
            "NLMCA-004", "ImageGallery", "Timing-entry count matches valid files", "unverified",
            {"time_config_count_equals_valid_file_count": True}, None,
            {"gallery_locator": gallery_locator, **config_failure},
            operator_hint=INVALID_TIME_CONFIG_HINT if invalid_time_config else None,
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
            operator_hint=TIME_CONFIG_EXCEEDS_FILES_HINT if config_count > file_count else None,
        ))

    subtitle_check = saved_subtitle_check(
        time_config if not config_failure else None,
        gallery_locator,
        section_studio_url=section_studio_url,
        config_failure=config_failure,
    )
    heading_order_check = subtitle_heading_order_check(
        time_config if not config_failure else None,
        gallery_locator,
        section_studio_url=section_studio_url,
    )
    bold_check = subtitle_bold_check(
        time_config if not config_failure else None,
        gallery_locator,
        section_studio_url=section_studio_url,
    )

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
        exc.completed_checks = list(checks) + [subtitle_check, heading_order_check, bold_check]
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
    checks.append(subtitle_check)
    checks.append(heading_order_check)
    checks.append(bold_check)
    return checks


def saved_subtitle_check(
    time_config: Any,
    gallery_locator: str,
    *,
    section_studio_url: str | None,
    config_failure: dict[str, Any] | None = None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"gallery_locator": gallery_locator}

    def unverified_check(details: dict[str, Any]) -> dict[str, Any]:
        evidence.update(details)
        return check_result(
            "NLMCA-010", "ImageGallery", SAVED_SUBTITLE_CHECK_NAME, "unverified",
            {"all_subtitles_readable_without_blockquote_prefixes": True}, None, evidence,
        )

    if config_failure:
        return unverified_check(config_failure)

    if not isinstance(time_config, list) or not time_config:
        return unverified_check({
            "failure_stage": "get_play_config",
            "failure_category": "invalid_response",
            "summary": "Studio play_config.time_config has no readable subtitle records.",
        })

    page_counts: dict[int, int] = {}
    for index, item in enumerate(time_config, start=1):
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("subtitle"), str)
            or not item["subtitle"].strip()
        ):
            return unverified_check({
                "failure_stage": "get_play_config",
                "failure_category": "invalid_response",
                "summary": "Studio play_config.time_config has one or more missing or unreadable subtitle values.",
            })
        page_number = subtitle_page_number(item, index)
        decoded_subtitle = html.unescape(item["subtitle"])
        marker_count = sum(match.group(0).count(">") for match in BLOCKQUOTE_PREFIX_RE.finditer(decoded_subtitle))
        if marker_count:
            page_counts[page_number] = page_counts.get(page_number, 0) + marker_count

    affected_pages = [
        {"page": page, "marker_count": marker_count}
        for page, marker_count in sorted(page_counts.items())
    ]
    marker_total = sum(page_counts.values())
    if marker_total:
        summary = (
            f"Saved Gallery subtitle text contains {marker_total} line-leading Markdown blockquote marker(s) "
            f"on {len(affected_pages)} page(s). This check does not inspect existing MP3 audio."
        )
        if section_studio_url:
            summary += f" [Open Studio section]({section_studio_url})"
        evidence.update({
            "affected_pages": affected_pages,
            "total_marker_count": marker_total,
            "summary": summary,
        })
        return check_result(
            "NLMCA-010", "ImageGallery", SAVED_SUBTITLE_CHECK_NAME, "issue",
            {"all_subtitles_readable_without_blockquote_prefixes": True},
            {"affected_page_count": len(affected_pages), "marker_count": marker_total}, evidence,
        )

    evidence.update({
        "affected_pages": [],
        "total_marker_count": 0,
        "summary": "Every saved Gallery subtitle was readable; no line-leading Markdown blockquote prefixes were found.",
    })
    return check_result(
        "NLMCA-010", "ImageGallery", SAVED_SUBTITLE_CHECK_NAME, "pass",
        {"all_subtitles_readable_without_blockquote_prefixes": True},
        {"affected_page_count": 0, "marker_count": 0}, evidence,
    )


def subtitle_page_number(item: dict[str, Any], index: int) -> int:
    page = item.get("page")
    return page if isinstance(page, int) and not isinstance(page, bool) and page > 0 else index


def markdown_block_types(markdown: str) -> list[str]:
    """Return the top-level block types used by the subtitle-order check."""
    blocks: list[str] = []
    for token in SUBTITLE_MARKDOWN.parse(markdown):
        if token.level != 0:
            continue
        if token.type == "heading_open":
            blocks.append("heading")
        elif token.type == "paragraph_open":
            blocks.append("paragraph")
        elif token.nesting == 1 or token.type in {"code_block", "fence", "hr", "html_block"}:
            blocks.append("other")
    return blocks


def subtitle_heading_order_check(
    time_config: Any,
    gallery_locator: str,
    *,
    section_studio_url: str | None,
) -> dict[str, Any]:
    evidence: dict[str, Any] = {"gallery_locator": gallery_locator}
    expected = SUBTITLE_HEADING_ORDER_EXPECTED

    eligible_count = 0
    affected_pages: dict[int, int] = {}
    if isinstance(time_config, list):
        for index, item in enumerate(time_config, start=1):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("subtitle"), str)
                or not item["subtitle"].strip()
            ):
                continue
            eligible_count += 1
            page_number = subtitle_page_number(item, index)
            blocks = markdown_block_types(item["subtitle"])
            if blocks and blocks[0] == "paragraph":
                later_heading_count = sum(block == "heading" for block in blocks[1:])
                if later_heading_count:
                    affected_pages.setdefault(page_number, later_heading_count)

    affected = [
        {"page": page, "opening_block": "paragraph", "later_heading_count": heading_count}
        for page, heading_count in sorted(affected_pages.items())
    ]
    affected_count = len(affected)
    status = "issue" if affected_count else "pass"
    if affected_count:
        page_list = ", ".join(str(item["page"]) for item in affected)
        summary = (
            f"A paragraph precedes a Markdown heading on {affected_count} of {eligible_count} readable subtitle page(s): "
            f"page {page_list}."
        )
    else:
        summary = "No readable subtitle page begins with a paragraph followed by a Markdown heading."
    if section_studio_url and affected_count:
        summary += f" [Open Studio section]({section_studio_url})"
    evidence.update({
        "eligible_page_count": eligible_count,
        "affected_pages": affected,
        "affected_page_count": affected_count,
        "summary": summary,
    })
    return check_result(
        "NLMCA-011", "ImageGallery", SUBTITLE_HEADING_ORDER_CHECK_NAME, status,
        expected,
        {"eligible_page_count": eligible_count, "affected_page_count": affected_count},
        evidence,
        operator_hint=SUBTITLE_HEADING_ORDER_HINT if affected_count else None,
    )


def unescaped_bold_candidate_count(markdown: str) -> int:
    candidate_count = 0
    index = 0
    while index < len(markdown):
        if markdown[index] == "\\":
            index += 2
            continue
        if markdown[index] == "`":
            run_end = index + 1
            while run_end < len(markdown) and markdown[run_end] == "`":
                run_end += 1
            run_length = run_end - index
            closing_index = run_end
            while closing_index < len(markdown):
                if markdown[closing_index] == "\\":
                    closing_index += 2
                    continue
                if markdown[closing_index] == "`":
                    closing_end = closing_index + 1
                    while closing_end < len(markdown) and markdown[closing_end] == "`":
                        closing_end += 1
                    if closing_end - closing_index == run_length:
                        index = closing_end
                        break
                    closing_index = closing_end
                    continue
                closing_index += 1
            else:
                index = run_end
            continue
        if markdown.startswith("**", index):
            run_end = index + 2
            while run_end < len(markdown) and markdown[run_end] == "*":
                run_end += 1
            candidate_count += (run_end - index) // 2
            index = run_end
            continue
        index += 1
    return candidate_count


def markdown_bold_delimiter_counts(markdown: str) -> tuple[int, int]:
    candidate_count = 0
    recognized_count = 0
    for token in SUBTITLE_MARKDOWN.parse(markdown):
        if token.type != "inline":
            continue
        candidate_count += unescaped_bold_candidate_count(token.content)
        recognized_count += sum(
            child.type in {"strong_open", "strong_close"} and child.markup == "**"
            for child in token.children or []
        )
    return candidate_count, recognized_count


def subtitle_bold_check(
    time_config: Any,
    gallery_locator: str,
    *,
    section_studio_url: str | None,
) -> dict[str, Any]:
    eligible_count = 0
    affected_pages: dict[int, int] = {}
    if isinstance(time_config, list):
        for index, item in enumerate(time_config, start=1):
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("subtitle"), str)
                or not item["subtitle"].strip()
            ):
                continue
            eligible_count += 1
            candidate_count, recognized_count = markdown_bold_delimiter_counts(item["subtitle"])
            unrecognized_count = candidate_count - recognized_count
            if unrecognized_count > 0:
                page_number = subtitle_page_number(item, index)
                affected_pages[page_number] = affected_pages.get(page_number, 0) + unrecognized_count

    affected = [
        {"page": page, "unrecognized_bold_count": count}
        for page, count in sorted(affected_pages.items())
    ]
    affected_count = len(affected)
    unrecognized_total = sum(affected_pages.values())
    if affected_count:
        page_list = ", ".join(str(item["page"]) for item in affected)
        summary = (
            f"CommonMark did not recognize {unrecognized_total} unescaped ** candidate(s) as strong delimiters "
            f"on {affected_count} of {eligible_count} readable subtitle page(s): page {page_list}."
        )
        if section_studio_url:
            summary += f" [Open Studio section]({section_studio_url})"
    else:
        summary = "Every readable saved Gallery subtitle page has CommonMark-recognized bold delimiters."
    evidence = {
        "gallery_locator": gallery_locator,
        "eligible_page_count": eligible_count,
        "affected_pages": affected,
        "affected_page_count": affected_count,
        "unrecognized_bold_count": unrecognized_total,
        "summary": summary,
    }
    return check_result(
        "NLMCA-012", "ImageGallery", SUBTITLE_BOLD_CHECK_NAME,
        "issue" if affected_count else "pass",
        {"all_unescaped_double_asterisk_candidates_form_strong_tokens": True},
        {
            "eligible_page_count": eligible_count,
            "affected_page_count": affected_count,
            "unrecognized_bold_count": unrecognized_total,
        },
        evidence,
    )


def section_checks(studio_reader: StudioReader, vertical: dict[str, Any]) -> list[dict[str, Any]]:
    vertical_locator = vertical_locator_of(vertical)
    if not vertical_locator:
        summary = "Studio course structure did not identify a valid vertical locator."
        checks = [
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
                ("NLMCA-010", "ImageGallery", SAVED_SUBTITLE_CHECK_NAME, {"all_subtitles_readable_without_blockquote_prefixes": True}),
                ("NLMCA-012", "ImageGallery", SUBTITLE_BOLD_CHECK_NAME, {"all_unescaped_double_asterisk_candidates_form_strong_tokens": True}),
                ("NLMCA-008", "组件顺序", "ImageGallery 位于目标小节首位", {"first_child_category": "imagesgallery"}),
                ("NLMCA-009", "组件顺序", "“本节要点”位于目标小节末尾", {"last_child_category": "html", "last_child_name": SUMMARY_COMPONENT_NAME}),
            )
        ]
        checks.append(subtitle_heading_order_check(
            None,
            "",
            section_studio_url=None,
        ))
        return checks
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
                    ("NLMCA-010", SAVED_SUBTITLE_CHECK_NAME, {"all_subtitles_readable_without_blockquote_prefixes": True}),
                    ("NLMCA-012", SUBTITLE_BOLD_CHECK_NAME, {"all_unescaped_double_asterisk_candidates_form_strong_tokens": True}),
                )
            ])
            gallery_data_checks.append(subtitle_heading_order_check(
                None,
                gallery_locator,
                section_studio_url=vertical_studio_url(studio_reader.studio_base, vertical_locator),
            ))
            gallery_data_checks.append(subtitle_bold_check(
                None,
                gallery_locator,
                section_studio_url=vertical_studio_url(studio_reader.studio_base, vertical_locator),
            ))
            continue
        try:
            gallery_data_checks.extend(gallery_checks(
                studio_reader,
                gallery_locator,
                vertical_studio_url(studio_reader.studio_base, vertical_locator),
            ))
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
        "schema_version": "1.1.0",
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
