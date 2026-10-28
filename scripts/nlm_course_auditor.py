from __future__ import annotations

import argparse
import json
import os
import re
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol, TextIO
from urllib.parse import urlparse, urlunparse

import requests


COURSE_KEY_PATTERN = re.compile(r"^course-v1:[^+\s<>:\"/\\|?*\x00-\x1f]+(?:\+[^+\s<>:\"/\\|?*\x00-\x1f]+){2}$")
TARGET_VERTICAL_NAME = "赋能内容"
SUMMARY_COMPONENT_NAME = "本节要点"
REQUEST_TIMEOUT = 60
GALLERY_READ_HANDLERS = {"get_files", "get_play_config", "get_audios"}


class AuditError(RuntimeError):
    """An input or global read failure safe to show to the operator."""


class StudioReadError(AuditError):
    def __init__(self, stage: str, summary: str, http_status: int | None = None):
        super().__init__(summary)
        self.stage = stage
        self.http_status = http_status
        self.safe_summary = summary


class StudioReader(Protocol):
    studio_base: str

    def get_course_structure(self, course_id: str) -> Any: ...

    def get_files(self, gallery_locator: str) -> Any: ...

    def get_play_config(self, gallery_locator: str) -> Any: ...

    def get_audios(self, gallery_locator: str) -> Any: ...


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def validate_course_key(course_id: str) -> None:
    if not isinstance(course_id, str) or not COURSE_KEY_PATTERN.fullmatch(course_id):
        raise AuditError("Provide a complete course-v1:ORG+COURSE+RUN course key.")


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
        if isinstance(value, str) and value.strip():
            return value.strip()
    return ""


def display_name_of(node: dict[str, Any]) -> str:
    value = node.get("display_name")
    return value if isinstance(value, str) else ""


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
    http_status = getattr(exc, "http_status", None)
    evidence: dict[str, Any] = {
        "failure_stage": stage,
        "failure_category": "read_failure",
        "summary": getattr(exc, "safe_summary", "Studio could not provide a verifiable response."),
    }
    if isinstance(http_status, int):
        evidence["http_status"] = http_status
    return evidence


def gallery_checks(studio_reader: StudioReader, gallery_locator: str) -> list[dict[str, Any]]:
    checks: list[dict[str, Any]] = []
    files: list[dict[str, Any]] | None = None
    total_count: int | None = None
    file_failure: dict[str, Any] | None = None
    try:
        payload = studio_reader.get_files(gallery_locator)
        files, total_count = safe_file_records(payload)
        if files is None:
            file_failure = {
                "failure_stage": "get_files",
                "failure_category": "invalid_response",
                "summary": "Studio get_files did not return a complete list of uniquely identified file records.",
            }
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
        config_payload = studio_reader.get_play_config(gallery_locator)
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
        audio_payload = studio_reader.get_audios(gallery_locator)
        audios = audio_payload.get("audios") if isinstance(audio_payload, dict) else None
        if not isinstance(audios, list) or any(not isinstance(item, dict) for item in audios):
            audio_failure = {
                "failure_stage": "get_audios",
                "failure_category": "invalid_response",
                "summary": "Studio get_audios.audios is not a list of bindings.",
            }
        else:
            audio_failure = None
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
    vertical_locator = locator_of(vertical)
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
    for gallery in galleries:
        gallery_locator = locator_of(gallery)
        if not gallery_locator:
            checks.extend([
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
        checks.extend(gallery_checks(studio_reader, gallery_locator))
    return checks


def aggregate_status(checks: list[dict[str, Any]], course_findings: list[dict[str, Any]]) -> tuple[str, int, int, int]:
    statuses = [check["status"] for check in checks]
    statuses.extend(finding["status"] for finding in course_findings)
    issue_count = statuses.count("issue")
    unverified_count = statuses.count("unverified")
    pass_count = statuses.count("pass")
    status = "issue" if issue_count else "unverified" if unverified_count else "pass"
    return status, pass_count, issue_count, unverified_count


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
    course_tree = studio_reader.get_course_structure(course_id)
    if not isinstance(course_tree, dict):
        raise AuditError("Studio returned an invalid course-structure response.")

    safe_course_id = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", course_id).rstrip(" .")
    output_dir = Path(working_directory) / "auditor" / safe_course_id
    atomic_write_json(output_dir / "course-structure.json", course_tree)

    target_sections: list[dict[str, Any]] = []
    all_checks: list[dict[str, Any]] = []
    for vertical in find_target_verticals(course_tree):
        checks = section_checks(studio_reader, vertical)
        section = {
            "name": display_name_of(vertical),
            "locator": locator_of(vertical),
            "studio_url": f"{studio_reader.studio_base.rstrip('/')}/container/{locator_of(vertical)}",
            "checks": checks,
        }
        target_sections.append(section)
        all_checks.extend(checks)

    course_findings: list[dict[str, Any]] = []
    if not target_sections:
        course_findings.append({
            "check_id": "NLMCA-006",
            "category": "课程范围",
            "name": "课程结构中找到赋能内容目标小节",
            "status": "unverified",
            "expected": {"target_section_count": "at least 1"},
            "actual": {"target_section_count": 0},
            "evidence": {
                "failure_stage": "target_discovery",
                "failure_category": "no_target_sections",
                "summary": "No matching target vertical was found; confirm the tenant, course, and Studio naming.",
            },
        })

    status, pass_count, issue_count, unverified_count = aggregate_status(all_checks, course_findings)
    report = {
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
    atomic_write_json(output_dir / "report.json", report)
    print(json.dumps(report, ensure_ascii=False, indent=2), file=output or sys.stdout)
    return report


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        item = part.strip()
        if "=" in item:
            name, value = item.split("=", 1)
            if name.strip():
                cookies[name.strip()] = value.strip()
    return cookies


def derive_courses_base(studio_base: str) -> str:
    parsed = urlparse(studio_base)
    host = parsed.hostname or ""
    if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
        raise AuditError("Tenant configuration must contain an HTTP(S) Studio base URL without embedded credentials.")
    if host.startswith("studio."):
        host = "courses." + host[len("studio.") :]
    else:
        raise AuditError("Tenant configuration must provide courses_base when its Studio host does not start with studio.")
    if parsed.port:
        host += f":{parsed.port}"
    return urlunparse((parsed.scheme, host, "", "", "", ""))


class StudioApiReader:
    """Authenticated reader with an explicit allowlist of Studio read operations."""

    def __init__(
        self,
        *,
        studio_base: str,
        courses_base: str,
        environ: dict[str, str] | None = None,
    ):
        self.studio_base = studio_base.rstrip("/")
        self.host = urlparse(self.studio_base).hostname or ""
        self.environ = os.environ if environ is None else environ
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })
        self.csrf_token = self.environ.get("FIRA_SAAS_OP_CSRF_TOKEN", "").strip()
        cookie_header = self.environ.get("FIRA_SAAS_OP_COOKIE", "").strip()
        cookies = parse_cookie_header(cookie_header)
        for key, value in cookies.items():
            self.session.cookies.set(key, value, domain=self.host)
        if not self.csrf_token:
            self.csrf_token = cookies.get("csrftoken", "")

        account = self.environ.get("FIRA_SAAS_OP_ACCOUNT", "").strip()
        password = self.environ.get("FIRA_SAAS_OP_PASSWORD", "").strip()
        if cookies:
            return
        if account or password:
            if not account or not password:
                raise AuditError("Configure both FIRA_SAAS_OP_ACCOUNT and FIRA_SAAS_OP_PASSWORD.")
            self._login(account, password, courses_base)
            return
        raise AuditError(
            "Authentication is required. Configure FIRA_SAAS_OP_ACCOUNT and FIRA_SAAS_OP_PASSWORD, "
            "or FIRA_SAAS_OP_COOKIE with FIRA_SAAS_OP_CSRF_TOKEN."
        )

    def _login(self, account: str, password: str, courses_base: str) -> None:
        try:
            response = self.session.post(
                f"{courses_base.rstrip('/')}/openapi/v1/auth/login/",
                data={"account": account, "password": password},
                timeout=30,
            )
            if int(getattr(response, "status_code", 200)) >= 400:
                raise StudioReadError("authentication", "Studio login failed.", int(response.status_code))
            payload = response.json()
        except StudioReadError:
            raise
        except (requests.RequestException, ValueError, json.JSONDecodeError) as exc:
            raise StudioReadError("authentication", "Studio login failed.") from exc
        jwt = payload.get("meta", {}).get("jwt") if isinstance(payload, dict) else None
        if not isinstance(jwt, str) or not jwt:
            raise StudioReadError("authentication", "Studio login did not return an authenticated session.")
        self.session.headers["Authorization"] = f"Bearer {jwt}"

    def _json_response(self, response: Any, stage: str) -> Any:
        status = int(getattr(response, "status_code", 200))
        if status >= 400:
            raise StudioReadError(stage, f"Studio read failed with HTTP {status}.", status)
        try:
            return response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise StudioReadError(stage, "Studio returned invalid JSON.", status) from exc

    def _csrf(self) -> str:
        if self.csrf_token:
            return self.csrf_token
        for cookie in self.session.cookies:
            if cookie.name == "csrftoken" and self.host in (cookie.domain or ""):
                self.csrf_token = cookie.value
                return self.csrf_token
        for cookie in self.session.cookies:
            if cookie.name == "csrftoken":
                self.csrf_token = cookie.value
                return self.csrf_token
        raise AuditError("The authenticated Studio session has no CSRF token for Gallery read handlers.")

    def get_course_structure(self, course_id: str) -> Any:
        try:
            response = self.session.get(
                f"{self.studio_base}/course/{course_id}",
                params={"format": "concise"},
                headers={"Accept": "application/json, text/plain, */*"},
                timeout=REQUEST_TIMEOUT,
            )
        except requests.RequestException as exc:
            raise StudioReadError("course_structure", "Studio course structure could not be read.") from exc
        return self._json_response(response, "course_structure")

    def _post_gallery_read(self, gallery_locator: str, handler: str, body: dict[str, Any]) -> Any:
        if handler not in GALLERY_READ_HANDLERS:
            raise AuditError("Only the approved Gallery read handlers are available.")
        path = f"/preview/xblock/{gallery_locator}/handler/{handler}"
        try:
            response = self.session.post(
                f"{self.studio_base}{path}",
                json=body,
                headers={
                    "Accept": "application/json, text/plain, */*",
                    "Content-Type": "application/json",
                    "X-CSRFToken": self._csrf(),
                    "X-Requested-With": "XMLHttpRequest",
                    "Origin": self.studio_base,
                    "Referer": f"{self.studio_base}/container/{gallery_locator}",
                },
                timeout=REQUEST_TIMEOUT,
            )
        except StudioReadError:
            raise
        except requests.RequestException as exc:
            raise StudioReadError(handler, f"Studio {handler} read failed.") from exc
        return self._json_response(response, handler)

    def get_files(self, gallery_locator: str) -> Any:
        return self._post_gallery_read(gallery_locator, "get_files", {"current_page": 0, "page_size": 100})

    def get_play_config(self, gallery_locator: str) -> Any:
        return self._post_gallery_read(gallery_locator, "get_play_config", {})

    def get_audios(self, gallery_locator: str) -> Any:
        return self._post_gallery_read(gallery_locator, "get_audios", {})


def load_tenant(tenant: str, config_path: Path) -> tuple[str, str]:
    if not config_path.is_file():
        raise AuditError(f"Tenant configuration file was not found: {config_path}")
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AuditError("Tenant configuration file is not valid JSON.") from exc
    tenants = config.get("tenants") if isinstance(config, dict) else None
    profile = tenants.get(tenant) if isinstance(tenants, dict) else None
    if not isinstance(profile, dict):
        raise AuditError(f"Tenant profile '{tenant}' is not configured.")
    studio_base = profile.get("studio_base")
    courses_base = profile.get("courses_base")
    if not isinstance(studio_base, str) or not studio_base.strip():
        raise AuditError(f"Tenant profile '{tenant}' has no studio_base.")
    parsed = urlparse(studio_base)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname or parsed.username or parsed.password:
        raise AuditError(f"Tenant profile '{tenant}' has an invalid studio_base.")
    if courses_base is None or courses_base == "":
        courses_base = derive_courses_base(studio_base)
    if not isinstance(courses_base, str):
        raise AuditError(f"Tenant profile '{tenant}' has an invalid courses_base.")
    courses_parsed = urlparse(courses_base)
    if courses_parsed.scheme not in {"http", "https"} or not courses_parsed.hostname or courses_parsed.username or courses_parsed.password:
        raise AuditError(f"Tenant profile '{tenant}' has an invalid courses_base.")
    return studio_base.rstrip("/"), courses_base.rstrip("/")


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Read-only structural audit of a saved Studio course")
    parser.add_argument("--tenant", required=True, help="configured tenant profile key")
    parser.add_argument("--course-id", required=True, help="complete course-v1:ORG+COURSE+RUN key")
    parser.add_argument("--tenant-config", default="tenant-config.json", help="tenant base URL configuration JSON")
    parser.add_argument("--working-directory", default=".", help="directory under which audit artifacts are saved")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
        validate_course_key(args.course_id)
        studio_base, courses_base = load_tenant(args.tenant, Path(args.tenant_config).expanduser().resolve())
        reader = StudioApiReader(studio_base=studio_base, courses_base=courses_base)
        report = audit_course(
            tenant=args.tenant,
            course_id=args.course_id,
            studio_reader=reader,
            working_directory=Path(args.working_directory).expanduser().resolve(),
        )
        return 0 if report["status"] == "pass" else 1
    except AuditError as exc:
        print(json.dumps({"status": "failed", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
