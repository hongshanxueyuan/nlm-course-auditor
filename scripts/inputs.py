from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse, urlunparse

from .errors import AuditError


COURSE_KEY_PATTERN = re.compile(r"^course-v1:[^+\s<>:\"/\\|?*\x00-\x1f]+(?:\+[^+\s<>:\"/\\|?*\x00-\x1f]+){2}$")


SAFE_BLOCK_LOCATOR_PATTERN = re.compile(r"^block-v1:[A-Za-z0-9.+@_-]+$")


VERTICAL_LOCATOR_PATTERN = re.compile(
    r"^block-v1:([^+/\s]+)\+([^+/\s]+)\+([^+/\s]+)\+type@vertical\+block@([^/\s?#]+)$"
)


DEFAULT_TENANTS: dict[str, dict[str, str]] = {
    "main": {"studio_base": "https://studio.firacademy.com"},
}


def validate_course_key(course_id: str) -> None:
    if not isinstance(course_id, str) or not COURSE_KEY_PATTERN.fullmatch(course_id):
        raise AuditError("Provide a complete course-v1:ORG+COURSE+RUN course key.")


def url_host_key(value: str) -> tuple[str, int | None]:
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise AuditError("Studio URLs must use an HTTP(S) host configured in tenant profiles.")
    try:
        port = parsed.port
    except ValueError as exc:
        raise AuditError("Studio URL has an invalid port.") from exc
    if port == (443 if parsed.scheme == "https" else 80):
        port = None
    return parsed.hostname.casefold().rstrip("."), port


def parse_studio_course_url(studio_url: str) -> tuple[tuple[str, int], str]:
    if not isinstance(studio_url, str) or not studio_url.strip():
        raise AuditError("Provide a Studio course or vertical URL.")
    parsed = urlparse(studio_url.strip())
    if parsed.username or parsed.password:
        raise AuditError("Studio URLs cannot contain embedded credentials.")
    host = url_host_key(studio_url.strip())
    segments = [unquote(part) for part in parsed.path.split("/") if part]
    if len(segments) == 2 and segments[0] == "course":
        course_id = segments[1]
    elif len(segments) == 2 and segments[0] == "container":
        match = VERTICAL_LOCATOR_PATTERN.fullmatch(segments[1])
        if not match:
            raise AuditError("Studio section URLs must identify a vertical block.")
        course_id = "course-v1:" + "+".join(match.groups()[:3])
    else:
        raise AuditError("Use a Studio /course/course-v1:... or /container/block-v1:... vertical URL.")
    validate_course_key(course_id)
    return host, course_id


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


def validated_tenant_profile(tenant: str, profile: Any) -> tuple[str, str]:
    if not isinstance(profile, dict):
        raise AuditError(f"Tenant profile '{tenant}' is not configured.")
    studio_base = profile.get("studio_base")
    courses_base = profile.get("courses_base")
    if not isinstance(studio_base, str) or not studio_base.strip():
        raise AuditError(f"Tenant profile '{tenant}' has no studio_base.")
    parsed = urlparse(studio_base)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise AuditError(f"Tenant profile '{tenant}' has an invalid studio_base.")
    url_host_key(studio_base)
    if courses_base is None or courses_base == "":
        courses_base = derive_courses_base(studio_base)
    if not isinstance(courses_base, str):
        raise AuditError(f"Tenant profile '{tenant}' has an invalid courses_base.")
    courses_parsed = urlparse(courses_base)
    if (
        courses_parsed.scheme not in {"http", "https"}
        or not courses_parsed.hostname
        or courses_parsed.username
        or courses_parsed.password
        or courses_parsed.path not in {"", "/"}
        or courses_parsed.query
        or courses_parsed.fragment
    ):
        raise AuditError(f"Tenant profile '{tenant}' has an invalid courses_base.")
    url_host_key(courses_base)
    return studio_base.rstrip("/"), courses_base.rstrip("/")


def read_tenants(config_path: Path) -> dict[str, Any]:
    if not config_path.is_file():
        return {}
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise AuditError("Tenant configuration file is not valid JSON.") from exc
    tenants = config.get("tenants") if isinstance(config, dict) else None
    if not isinstance(tenants, dict) or not tenants:
        raise AuditError("Tenant configuration must contain a non-empty tenants map.")
    return tenants


def load_tenant(tenant: str, config_path: Path) -> tuple[str, str]:
    tenants = read_tenants(config_path)
    if tenant in tenants:
        profile = tenants[tenant]
    else:
        profile = DEFAULT_TENANTS.get(tenant)
    return validated_tenant_profile(tenant, profile)


def resolve_audit_input(
    *,
    tenant: str | None,
    course_id: str | None,
    studio_url: str | None,
    tenant_config_path: Path,
) -> tuple[str, str, str, str]:
    has_explicit_input = tenant is not None or course_id is not None
    if studio_url is not None:
        if has_explicit_input:
            raise AuditError("Choose either tenant/course key or one Studio URL input.")
        host_key, resolved_course_id = parse_studio_course_url(studio_url)
        tenants = read_tenants(tenant_config_path)

        def matching_profiles(profiles: dict[str, Any]) -> list[tuple[str, str, str]]:
            found: list[tuple[str, str, str]] = []
            for profile_name, profile in profiles.items():
                studio_base, courses_base = validated_tenant_profile(str(profile_name), profile)
                if url_host_key(studio_base) == host_key:
                    found.append((str(profile_name), studio_base, courses_base))
            return found

        matches = matching_profiles(tenants)
        if not matches:
            built_in_profiles = {
                name: profile
                for name, profile in DEFAULT_TENANTS.items()
                if name not in tenants
            }
            matches = matching_profiles(built_in_profiles)
        if not matches:
            raise AuditError("Studio URL host does not match a configured tenant profile.")
        if len(matches) != 1:
            raise AuditError("Studio URL host maps to more than one tenant profile.")
        resolved_tenant, studio_base, courses_base = matches[0]
        return resolved_tenant, resolved_course_id, studio_base, courses_base

    if tenant is None or course_id is None:
        raise AuditError("Provide both --tenant and a complete --course-id, or use --studio-url.")
    if not isinstance(tenant, str) or not tenant.strip():
        raise AuditError("Provide a configured tenant profile name.")
    validate_course_key(course_id)
    studio_base, courses_base = load_tenant(tenant, tenant_config_path)
    return tenant, course_id, studio_base, courses_base
