from __future__ import annotations

import json
import os
from typing import Any, Callable
from urllib.parse import urlparse

import requests

from .errors import AuditAuthenticationError, AuditError, StudioReadError
from .inputs import derive_courses_base


REQUEST_TIMEOUT = 60


GALLERY_READ_HANDLERS = {"get_files", "get_play_config", "get_audios"}


AUTH_ERROR_PHRASES = (
    "not login yet",
    "not logged in",
    "unauthorized",
    "authentication required",
    "login required",
    "session expired",
    "csrf token expired",
    "csrf failed",
    "csrf failure",
    "invalid csrf",
)


def parse_cookie_header(cookie_header: str) -> dict[str, str]:
    cookies: dict[str, str] = {}
    for part in cookie_header.split(";"):
        item = part.strip()
        if "=" in item:
            name, value = item.split("=", 1)
            if name.strip():
                cookies[name.strip()] = value.strip()
    return cookies


class StudioApiReader:
    """Authenticated reader with an explicit allowlist of Studio read operations."""

    def __init__(
        self,
        *,
        studio_base: str,
        courses_base: str,
        course_id: str,
        environ: dict[str, str] | None = None,
        session_factory: Callable[[], Any] | None = None,
    ):
        self.studio_base = studio_base.rstrip("/")
        self.host = urlparse(self.studio_base).hostname or ""
        self.course_id = course_id
        self.environ = os.environ if environ is None else environ
        self.courses_base = courses_base.rstrip("/")
        self.session_factory = session_factory or requests.Session
        self.session: Any
        self.csrf_token = ""
        self._new_session()
        self._authenticate()

    def _new_session(self) -> None:
        self.session = self.session_factory()
        self.session.headers.update({
            "User-Agent": "Mozilla/5.0",
            "Accept": "application/json, text/javascript, */*; q=0.01",
        })

    def _csrf_cookie(self) -> str:
        for cookie in self.session.cookies:
            domain = str(getattr(cookie, "domain", "")).lstrip(".").casefold()
            if cookie.name == "csrftoken" and (domain == self.host.casefold() or self.host.casefold().endswith("." + domain)):
                return cookie.value
        return ""

    def _authenticate(self) -> None:
        self.csrf_token = self.environ.get("FIRA_SAAS_OP_CSRF_TOKEN", "").strip()
        cookie_header = self.environ.get("FIRA_SAAS_OP_COOKIE", "").strip()
        cookies = parse_cookie_header(cookie_header)
        for key, value in cookies.items():
            self.session.cookies.set(key, value, domain=self.host)
        if not self.csrf_token:
            self.csrf_token = cookies.get("csrftoken", "")
        if cookies:
            if not self.csrf_token:
                raise AuditAuthenticationError("Configure a matching CSRF token with the Studio Cookie.")
            return
        account = self.environ.get("FIRA_SAAS_OP_ACCOUNT", "").strip()
        password = self.environ.get("FIRA_SAAS_OP_PASSWORD", "").strip()
        if account or password:
            if not account or not password:
                raise AuditAuthenticationError("Configure both FIRA_SAAS_OP_ACCOUNT and FIRA_SAAS_OP_PASSWORD.")
            self._login(account, password)
            return
        raise AuditAuthenticationError(
            "Authentication is required. Configure FIRA_SAAS_OP_ACCOUNT and FIRA_SAAS_OP_PASSWORD, "
            "or FIRA_SAAS_OP_COOKIE with FIRA_SAAS_OP_CSRF_TOKEN."
        )

    def refresh_authentication(self) -> None:
        self._new_session()
        self._authenticate()

    def _login(self, account: str, password: str) -> None:
        try:
            response = self.session.post(
                f"{self.courses_base}/openapi/v1/auth/login/",
                data={"account": account, "password": password},
                timeout=30,
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 200))
            if status >= 300:
                raise AuditAuthenticationError("Studio login failed.", status)
            payload = response.json()
        except AuditAuthenticationError:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise AuditAuthenticationError("Studio login could not be completed.", status) from exc
        jwt = payload.get("meta", {}).get("jwt") if isinstance(payload, dict) else None
        if not isinstance(jwt, str) or not jwt:
            raise AuditAuthenticationError("Studio login did not return an authenticated session.")
        self.session.headers["Authorization"] = f"Bearer {jwt}"
        try:
            response = self.session.get(
                f"{self.studio_base}/course/{self.course_id}",
                headers={"Accept": "text/html,*/*"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
            )
            status = int(getattr(response, "status_code", 200))
            if status >= 300:
                raise AuditAuthenticationError("Studio session could not be established.", status)
        except AuditAuthenticationError:
            raise
        except Exception as exc:
            status = getattr(getattr(exc, "response", None), "status_code", None)
            raise AuditAuthenticationError("Studio session could not be established.", status) from exc
        self.csrf_token = self.environ.get("FIRA_SAAS_OP_CSRF_TOKEN", "").strip() or self._csrf_cookie()
        if not self.csrf_token:
            raise AuditAuthenticationError("Studio session did not provide a CSRF token.")

    def _json_response(self, response: Any, stage: str) -> Any:
        status = int(getattr(response, "status_code", 200))
        if status >= 400 or 300 <= status < 400:
            raise StudioReadError(
                stage,
                f"Studio {stage} read failed with HTTP {status}.",
                status,
                auth_failure=300 <= status < 400 or status in {401, 403},
            )
        try:
            payload = response.json()
        except (ValueError, json.JSONDecodeError) as exc:
            raise StudioReadError(stage, "Studio returned invalid JSON.", status) from exc
        if isinstance(payload, dict):
            auth_messages = [payload.get(key) for key in ("error", "error_message", "message", "msg")]
            if any(
                isinstance(message, str)
                and any(phrase in message.casefold() for phrase in AUTH_ERROR_PHRASES)
                for message in auth_messages
            ):
                raise StudioReadError(
                    stage,
                    "Studio authentication was rejected.",
                    status,
                    auth_failure=True,
                )
        return payload

    def _csrf(self) -> str:
        if self.csrf_token:
            return self.csrf_token
        self.csrf_token = self._csrf_cookie()
        if self.csrf_token:
            return self.csrf_token
        raise AuditAuthenticationError("The authenticated Studio session has no CSRF token for Gallery read handlers.")

    def get_course_structure(self, course_id: str) -> Any:
        if course_id != self.course_id:
            raise AuditError("The Studio reader course does not match the validated course key.")
        try:
            response = self.session.get(
                f"{self.studio_base}/course/{course_id}",
                params={"format": "concise"},
                headers={"Accept": "application/json, text/plain, */*"},
                timeout=REQUEST_TIMEOUT,
                allow_redirects=False,
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
                allow_redirects=False,
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
