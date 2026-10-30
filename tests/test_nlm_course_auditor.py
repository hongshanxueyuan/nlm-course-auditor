import json
from urllib.parse import urlparse
import pytest

from scripts import nlm_course_auditor as auditor
from scripts.nlm_course_auditor import StudioReadError, audit_course


COURSE_KEY = "course-v1:FIRAx+1040663+20261018"
VERTICAL = "block-v1:FIRAx+1040663+20261018+type@vertical+block@lesson1"
GALLERY = "block-v1:FIRAx+1040663+20261018+type@imagesgallery+block@gallery1"


class FakeStudioReader:
    studio_base = "https://studio.example.test"

    def __init__(self, *, course_tree=None, gallery_overrides=None, course_error=None):
        self.course_tree = course_tree or course_tree_with_one_target()
        self.gallery_overrides = gallery_overrides or {}
        self.course_error = course_error
        self.calls = []

    def get_course_structure(self, course_id):
        self.calls.append(("get_course_structure", course_id))
        if self.course_error:
            raise self.course_error
        return self.course_tree

    def _gallery_response(self, name, default):
        response = self.gallery_overrides.get(name, default)
        if isinstance(response, BaseException):
            raise response
        return response

    def get_files(self, gallery_locator):
        self.calls.append(("get_files", gallery_locator))
        return self._gallery_response(
            "files",
            {
                "files": [
                    {"id": "asset-1", "filename": "page-one.png", "url": "https://media.test/private?sig=secret"},
                    {"id": "asset-2", "filename": "page-two.png", "url": "https://media.test/private?sig=secret"},
                ],
                "total_count": 2,
            },
        )

    def get_play_config(self, gallery_locator):
        self.calls.append(("get_play_config", gallery_locator))
        return self._gallery_response(
            "play_config", {"play_config": {"time_config": [{"page": 1}, {"page": 2}]}}
        )

    def get_audios(self, gallery_locator):
        self.calls.append(("get_audios", gallery_locator))
        return self._gallery_response("audios", {"audios": [{"id": "audio-secret"}]})


class RefreshingFakeStudioReader(FakeStudioReader):
    def __init__(self, *, auth_failures=0, always_auth_fail=False, **kwargs):
        super().__init__(**kwargs)
        self.auth_failures = auth_failures
        self.always_auth_fail = always_auth_fail
        self.refresh_count = 0

    def get_files(self, gallery_locator):
        if self.always_auth_fail or self.auth_failures:
            self.calls.append(("get_files", gallery_locator))
            if not self.always_auth_fail:
                self.auth_failures -= 1
            raise StudioReadError("get_files", "Studio authentication failed.", 401)
        return super().get_files(gallery_locator)

    def refresh_authentication(self):
        self.refresh_count += 1


class FakeReaderFactory:
    def __init__(self, reader):
        self.reader = reader
        self.calls = []

    def __call__(self, **kwargs):
        self.calls.append(kwargs)
        return self.reader


class FakeCookie:
    def __init__(self, name, value, domain):
        self.name = name
        self.value = value
        self.domain = domain


class FakeCookieJar:
    def __init__(self):
        self.values = {}

    def set(self, name, value, domain):
        self.values[name] = FakeCookie(name, value, domain)

    def __iter__(self):
        return iter(self.values.values())


class FakeHttpResponse:
    def __init__(self, payload=None, status_code=200):
        self.payload = payload
        self.status_code = status_code

    def json(self):
        return self.payload


class FakeHttpSession:
    def __init__(self, *, sessions, course_tree, auth_failures=None):
        self.headers = {}
        self.cookies = FakeCookieJar()
        self.sessions = sessions
        self.course_tree = course_tree
        self.auth_failures = auth_failures if auth_failures is not None else [0]
        self.calls = []
        sessions.append(self)

    def post(self, url, **kwargs):
        parsed = urlparse(url)
        self.calls.append(("POST", parsed.netloc, parsed.path, kwargs))
        if parsed.path == "/openapi/v1/auth/login/":
            return FakeHttpResponse({"meta": {"jwt": "jwt-secret"}})
        if parsed.path.endswith("/handler/get_files"):
            if self.auth_failures[0] > 0:
                self.auth_failures[0] -= 1
                return FakeHttpResponse({"error": "Not Login yet"})
            return FakeHttpResponse({"files": [{"id": "asset-1"}, {"id": "asset-2"}], "total_count": 2})
        if parsed.path.endswith("/handler/get_play_config"):
            return FakeHttpResponse({"play_config": {"time_config": [{"page": 1}, {"page": 2}]}})
        if parsed.path.endswith("/handler/get_audios"):
            return FakeHttpResponse({"audios": [{"id": "audio-secret"}]})
        raise AssertionError(f"Unexpected POST path: {parsed.path}")

    def get(self, url, **kwargs):
        parsed = urlparse(url)
        params = kwargs.get("params")
        self.calls.append(("GET", parsed.netloc, parsed.path, kwargs))
        if params == {"format": "concise"}:
            return FakeHttpResponse(self.course_tree)
        self.cookies.set("csrftoken", "landing-csrf-secret", domain=parsed.hostname)
        return FakeHttpResponse({})


def tenant_config(tmp_path, tenants=None):
    config_path = tmp_path / "tenant-config.json"
    config_path.parent.mkdir(parents=True, exist_ok=True)
    config_path.write_text(
        json.dumps({"tenants": tenants or {"uat": {"studio_base": FakeStudioReader.studio_base}}}),
        encoding="utf-8",
    )
    return config_path


def course_tree_with_one_target():
    return {
        "id": COURSE_KEY,
        "category": "course",
        "display_name": "培训课程",
        "child_info": {
            "children": [
                {
                    "id": "block-v1:FIRAx+1040663+20261018+type@chapter+block@chapter1",
                    "category": "chapter",
                    "display_name": "第一章",
                    "child_info": {
                        "children": [
                            {
                                "id": "block-v1:FIRAx+1040663+20261018+type@sequential+block@section1",
                                "category": "sequential",
                                "display_name": "第一节",
                                "child_info": {
                                    "children": [
                                        {
                                            "id": VERTICAL,
                                            "category": "vertical",
                                            "display_name": " 赋能内容 ",
                                            "child_info": {
                                                "children": [
                                                    {
                                                        "id": GALLERY,
                                                        "category": "imagesgallery",
                                                        "display_name": "课程讲解",
                                                    },
                                                    {
                                                        "id": "block-v1:FIRAx+1040663+20261018+type@html+block@summary",
                                                        "category": "html",
                                                        "display_name": " 本节要点 ",
                                                        "data": "<p>Do not include this body in the report.</p>",
                                                    },
                                                ]
                                            },
                                        },
                                        {
                                            "id": "block-v1:FIRAx+1040663+20261018+type@vertical+block@not-target",
                                            "category": "vertical",
                                            "display_name": "赋能内容扩展",
                                            "child_info": {"children": []},
                                        },
                                    ]
                                },
                            }
                        ]
                    },
                }
            ]
        },
    }


def test_complete_audit_reads_one_snapshot_saves_safe_report_and_prints(tmp_path, capsys):
    reader = FakeStudioReader()

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    assert reader.calls == [
        ("get_course_structure", COURSE_KEY),
        ("get_files", GALLERY),
        ("get_play_config", GALLERY),
        ("get_audios", GALLERY),
    ]
    assert report["schema_version"] == "1.0.0"
    assert report["tenant"] == "uat"
    assert report["course_id"] == COURSE_KEY
    assert report["status"] == "pass"
    assert report["pass_count"] == 7
    assert report["issue_count"] == 0
    assert report["unverified_count"] == 0
    assert report["course_findings"] == []
    assert len(report["target_sections"]) == 1

    section = report["target_sections"][0]
    assert section["name"] == " 赋能内容 "
    assert section["locator"] == VERTICAL
    assert section["studio_url"] == f"{reader.studio_base}/container/{VERTICAL}"
    assert [check["check_id"] for check in section["checks"]] == [
        "NLMCA-001",
        "NLMCA-002",
        "NLMCA-003",
        "NLMCA-004",
        "NLMCA-005",
        "NLMCA-008",
        "NLMCA-009",
    ]
    assert all(check["status"] == "pass" for check in section["checks"])
    assert section["checks"][2]["actual"] == {"valid_file_count": 2, "total_count": 2}
    assert section["checks"][3]["actual"] == {"time_config_count": 2, "valid_file_count": 2}
    assert section["checks"][4]["actual"] == {"audio_binding_count": 1}
    assert section["checks"][5]["actual"] == {
        "direct_child_count": 2,
        "imagesgallery_positions": [1],
        "first_child": {"position": 1, "category": "imagesgallery", "name": "课程讲解"},
    }
    assert section["checks"][6]["actual"] == {
        "direct_child_count": 2,
        "summary_positions": [2],
        "last_child": {"position": 2, "category": "html", "name": "本节要点"},
    }

    output_dir = tmp_path / "auditor" / COURSE_KEY.replace(":", "_")
    assert json.loads((output_dir / "course-structure.json").read_text(encoding="utf-8")) == reader.course_tree
    saved_report = json.loads((output_dir / "report.json").read_text(encoding="utf-8"))
    assert saved_report == report
    assert capsys.readouterr().out.strip() == json.dumps(report, ensure_ascii=False, indent=2)

    report_text = json.dumps(report, ensure_ascii=False)
    for private_value in ("page-one.png", "media.test", "audio-secret", "Do not include this body"):
        assert private_value not in report_text


def test_confirmed_component_and_gallery_count_violations_are_issues(tmp_path):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    blocks[:] = [
        {"id": GALLERY, "category": "imagesgallery", "display_name": "Gallery"},
        {"id": f"{GALLERY}-duplicate", "category": "imagesgallery", "display_name": "Gallery 2"},
        {"id": "block-v1:FIRAx+1040663+20261018+type@html+block@summary", "category": "html", "display_name": "本节要点"},
        {"id": "block-v1:FIRAx+1040663+20261018+type@html+block@summary2", "category": "html", "display_name": "本节要点"},
    ]
    reader = FakeStudioReader(
        course_tree=tree,
        gallery_overrides={
            "files": {"files": [{"id": "asset-1"}, {"id": "asset-2"}], "total_count": 3},
            "play_config": {"play_config": {"time_config": [{"page": 1}]}},
            "audios": {"audios": []},
        },
    )

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = report["target_sections"][0]["checks"]
    assert checks[0]["status"] == "issue"
    assert checks[0]["actual"] == {"count": 2}
    assert checks[1]["status"] == "issue"
    assert checks[1]["actual"] == {"count": 2}
    assert [check["status"] for check in checks[2:8]] == ["issue"] * 6
    assert [check["status"] for check in checks[8:]] == ["pass", "pass"]
    assert report["status"] == "issue"
    assert report["issue_count"] == 8
    assert report["unverified_count"] == 0


def test_other_direct_children_may_appear_between_required_components(tmp_path):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    gallery, summary = blocks
    blocks[:] = [
        gallery,
        {"id": "block-v1:FIRAx+1040663+20261018+type@problem+block@quiz", "category": "problem", "display_name": "测验"},
        summary,
    ]
    reader = FakeStudioReader(course_tree=tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {check["check_id"]: check for check in report["target_sections"][0]["checks"]}
    assert report["status"] == "pass"
    assert checks["NLMCA-008"]["actual"]["imagesgallery_positions"] == [1]
    assert checks["NLMCA-009"]["actual"]["summary_positions"] == [3]


def test_component_order_checks_report_wrong_boundaries_and_safe_positions(tmp_path):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    blocks[:] = [
        {
            "id": "block-v1:FIRAx+1040663+20261018+type@html+block@summary",
            "category": "html",
            "display_name": "本节要点",
            "data": "private html body",
        },
        {"id": GALLERY, "category": "imagesgallery", "display_name": "有声幻灯片"},
        {"id": "block-v1:FIRAx+1040663+20261018+type@problem+block@quiz", "category": "problem", "display_name": "测验"},
    ]
    reader = FakeStudioReader(course_tree=tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {check["check_id"]: check for check in report["target_sections"][0]["checks"]}
    assert checks["NLMCA-008"]["status"] == "issue"
    assert checks["NLMCA-008"]["actual"] == {
        "direct_child_count": 3,
        "imagesgallery_positions": [2],
        "first_child": {"position": 1, "category": "html", "name": "本节要点"},
    }
    assert checks["NLMCA-009"]["status"] == "issue"
    assert checks["NLMCA-009"]["actual"] == {
        "direct_child_count": 3,
        "summary_positions": [1],
        "last_child": {"position": 3, "category": "problem", "name": "测验"},
    }
    assert report["issue_count"] == 2
    assert report["target_sections"][0]["studio_url"].endswith(VERTICAL)
    assert "private html body" not in json.dumps(report, ensure_ascii=False)


@pytest.mark.parametrize("remaining_component", ["gallery", "summary"])
def test_missing_one_required_component_is_reported_separately_from_order(tmp_path, remaining_component):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    gallery, summary = blocks
    blocks[:] = [gallery if remaining_component == "gallery" else summary]
    reader = FakeStudioReader(course_tree=tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {check["check_id"]: check for check in report["target_sections"][0]["checks"]}
    assert report["issue_count"] == 2
    if remaining_component == "gallery":
        assert [checks[check_id]["status"] for check_id in ("NLMCA-001", "NLMCA-002", "NLMCA-008", "NLMCA-009")] == ["pass", "issue", "pass", "issue"]
        assert reader.calls == [
            ("get_course_structure", COURSE_KEY),
            ("get_files", GALLERY),
            ("get_play_config", GALLERY),
            ("get_audios", GALLERY),
        ]
    else:
        assert [checks[check_id]["status"] for check_id in ("NLMCA-001", "NLMCA-002", "NLMCA-008", "NLMCA-009")] == ["issue", "pass", "issue", "pass"]
        assert reader.calls == [("get_course_structure", COURSE_KEY)]


def test_nested_gallery_and_summary_do_not_count_as_direct_children(tmp_path):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    blocks[:] = [
        {
            "id": "block-v1:FIRAx+1040663+20261018+type@html+block@outer-gallery",
            "category": "html",
            "display_name": "文字讲解",
            "child_info": {"children": [{"id": GALLERY, "category": "imagesgallery", "display_name": "课程讲解"}]},
        },
        {
            "id": "block-v1:FIRAx+1040663+20261018+type@html+block@outer-summary",
            "category": "html",
            "display_name": "内容容器",
            "child_info": {
                "children": [
                    {
                        "id": "block-v1:FIRAx+1040663+20261018+type@html+block@nested-summary",
                        "category": "html",
                        "display_name": "本节要点",
                    }
                ]
            },
        },
    ]
    reader = FakeStudioReader(course_tree=tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {check["check_id"]: check for check in report["target_sections"][0]["checks"]}
    assert [checks[check_id]["status"] for check_id in ("NLMCA-001", "NLMCA-002", "NLMCA-008", "NLMCA-009")] == ["issue"] * 4
    assert report["issue_count"] == 4
    assert reader.calls == [("get_course_structure", COURSE_KEY)]


def test_missing_required_components_are_reported_as_issues(tmp_path):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    blocks.clear()
    reader = FakeStudioReader(course_tree=tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = report["target_sections"][0]["checks"]
    assert [check["check_id"] for check in checks] == ["NLMCA-001", "NLMCA-002", "NLMCA-008", "NLMCA-009"]
    assert [check["status"] for check in checks] == ["issue"] * 4
    assert [check["actual"] for check in checks[:2]] == [{"count": 0}, {"count": 0}]
    assert checks[2]["actual"]["direct_child_count"] == 0
    assert checks[3]["actual"]["direct_child_count"] == 0
    assert report["status"] == "issue"
    assert report["issue_count"] == 4
    assert reader.calls == [("get_course_structure", COURSE_KEY)]


def test_target_without_a_vertical_link_cannot_produce_section_issues(tmp_path):
    tree = course_tree_with_one_target()
    chapter = tree["child_info"]["children"][0]
    sequential = chapter["child_info"]["children"][0]
    vertical = sequential["child_info"]["children"][0]
    gallery = vertical["child_info"]["children"][0]
    vertical["id"] = "block-v1:FIRAx+1040663+20261018+type@chapter+block@not-a-vertical"
    vertical["child_info"]["children"] = [gallery]
    reader = FakeStudioReader(course_tree=tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    section = report["target_sections"][0]
    assert section["studio_url"] is None
    assert [check["status"] for check in section["checks"]] == ["unverified"] * 7
    assert report["status"] == "unverified"
    assert report["issue_count"] == 0
    assert report["unverified_count"] == 7
    assert reader.calls == [("get_course_structure", COURSE_KEY)]


def test_course_with_no_target_verticals_is_a_course_level_issue(tmp_path):
    reader = FakeStudioReader(course_tree={"id": COURSE_KEY, "category": "course", "child_info": {"children": []}})

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    assert report["status"] == "issue"
    assert report["target_sections"] == []
    assert report["course_findings"][0]["actual"] == {"target_section_count": 0}
    assert report["course_findings"][0]["status"] == "issue"
    assert report["issue_count"] == 1
    assert report["unverified_count"] == 0
    assert reader.calls == [("get_course_structure", COURSE_KEY)]


def test_each_rerun_fetches_a_fresh_snapshot_and_overwrites_report(tmp_path):
    first_tree = course_tree_with_one_target()
    second_tree = course_tree_with_one_target()
    second_tree["display_name"] = "Updated saved course"
    first_reader = FakeStudioReader(course_tree=first_tree)
    second_reader = FakeStudioReader(course_tree=second_tree)

    audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=first_reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )
    latest_report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=second_reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T02:02:03Z",
    )

    output_dir = tmp_path / "auditor" / COURSE_KEY.replace(":", "_")
    assert json.loads((output_dir / "course-structure.json").read_text(encoding="utf-8")) == second_tree
    assert json.loads((output_dir / "report.json").read_text(encoding="utf-8")) == latest_report
    assert latest_report["audited_at"] == "2026-10-28T02:02:03Z"
    assert first_reader.calls.count(("get_course_structure", COURSE_KEY)) == 1
    assert second_reader.calls.count(("get_course_structure", COURSE_KEY)) == 1


def test_gallery_read_failure_keeps_independent_checks_running_and_redacts_error(tmp_path):
    private_message = "page-secret.png https://media.example/item?sig=private-token"
    reader = RefreshingFakeStudioReader(
        gallery_overrides={"files": StudioReadError("get_files", private_message, 503)}
    )

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = report["target_sections"][0]["checks"]
    assert [check["status"] for check in checks] == ["pass", "pass", "unverified", "unverified", "pass", "pass", "pass"]
    assert checks[2]["evidence"]["http_status"] == 503
    assert reader.calls == [
        ("get_course_structure", COURSE_KEY),
        ("get_files", GALLERY),
        ("get_play_config", GALLERY),
        ("get_audios", GALLERY),
    ]
    assert reader.refresh_count == 0
    report_text = json.dumps(report, ensure_ascii=False)
    assert private_message not in report_text
    assert "page-secret.png" not in report_text
    assert "private-token" not in report_text


def test_confirmed_issue_takes_priority_over_independent_unverified_check_counts(tmp_path):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    blocks.pop()
    reader = FakeStudioReader(
        course_tree=tree,
        gallery_overrides={"files": {"files": [{"filename": "unidentified.png"}], "total_count": 1}},
    )

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    assert report["status"] == "issue"
    assert report["issue_count"] == 2
    assert report["unverified_count"] == 2
    assert report["pass_count"] == 3


def test_malformed_gallery_handlers_are_unverified_without_exposing_payloads(tmp_path):
    private_payload = "private-audio.mp3 https://media.example/private?signature=secret"
    reader = FakeStudioReader(
        gallery_overrides={
            "files": {"files": [{"id": "asset-1"}, {"filename": private_payload}], "total_count": 2},
            "play_config": {"play_config": {"time_config": private_payload}},
            "audios": {"audios": private_payload},
        }
    )

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = report["target_sections"][0]["checks"]
    assert [check["status"] for check in checks] == ["pass", "pass", "unverified", "unverified", "unverified", "pass", "pass"]
    assert report["status"] == "unverified"
    assert report["unverified_count"] == 3
    assert all(check["evidence"]["failure_category"] == "invalid_response" for check in checks[2:5])
    report_text = json.dumps(report, ensure_ascii=False)
    assert private_payload not in report_text
    assert "private-audio.mp3" not in report_text
    assert "signature=secret" not in report_text


def test_untrusted_block_locator_values_are_omitted_from_report_links(tmp_path):
    tree = course_tree_with_one_target()
    vertical = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]
    private_locator = "https://media.example/private?signature=locator-secret"
    vertical["id"] = private_locator
    vertical["child_info"]["children"][0]["id"] = private_locator
    reader = FakeStudioReader(course_tree=tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    section = report["target_sections"][0]
    assert section["locator"] == ""
    assert section["studio_url"] is None
    assert [check["status"] for check in section["checks"]] == ["unverified"] * 7
    report_text = json.dumps(report, ensure_ascii=False)
    assert private_locator not in report_text
    assert "locator-secret" not in report_text


def test_course_structure_failure_stops_and_replaces_stale_outputs_with_unverified_report(tmp_path):
    output_dir = tmp_path / "auditor" / COURSE_KEY.replace(":", "_")
    output_dir.mkdir(parents=True)
    (output_dir / "course-structure.json").write_text('{"stale": true}', encoding="utf-8")
    (output_dir / "report.json").write_text('{"stale": true}', encoding="utf-8")
    private_message = "server body includes page-secret.png and token=private-token"
    reader = FakeStudioReader(
        course_error=StudioReadError("course_structure", private_message, 502)
    )

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    finding = report["course_findings"][0]
    assert report["status"] == "unverified"
    assert report["target_sections"] == []
    assert report["unverified_count"] == 1
    assert finding["check_id"] == "NLMCA-007"
    assert finding["status"] == "unverified"
    assert finding["evidence"]["failure_stage"] == "course_structure"
    assert finding["evidence"]["http_status"] == 502
    assert reader.calls == [("get_course_structure", COURSE_KEY)]
    assert not (output_dir / "course-structure.json").exists()
    assert json.loads((output_dir / "report.json").read_text(encoding="utf-8")) == report
    report_text = json.dumps(report, ensure_ascii=False)
    assert private_message not in report_text
    assert "page-secret.png" not in report_text
    assert "private-token" not in report_text


def test_incomplete_course_tree_is_unverified_instead_of_a_zero_target_issue(tmp_path):
    reader = FakeStudioReader(course_tree={"id": COURSE_KEY, "category": "course"})

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    assert report["status"] == "unverified"
    assert report["issue_count"] == 0
    assert report["course_findings"][0]["check_id"] == "NLMCA-007"
    assert report["course_findings"][0]["evidence"]["failure_category"] == "invalid_response"
    assert not (tmp_path / "auditor" / COURSE_KEY.replace(":", "_") / "course-structure.json").exists()


def test_recoverable_auth_failure_refreshes_and_retries_the_read(tmp_path):
    reader = RefreshingFakeStudioReader(auth_failures=1)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    assert report["status"] == "pass"
    assert reader.refresh_count == 1
    assert reader.calls == [
        ("get_course_structure", COURSE_KEY),
        ("get_files", GALLERY),
        ("get_files", GALLERY),
        ("get_play_config", GALLERY),
        ("get_audios", GALLERY),
    ]


def test_unresolved_auth_failure_refreshes_only_to_a_bound_then_stops_run(tmp_path):
    tree = course_tree_with_one_target()
    blocks = tree["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"][0]["child_info"]["children"]
    blocks.pop()
    reader = RefreshingFakeStudioReader(course_tree=tree, always_auth_fail=True)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    finding = report["course_findings"][0]
    assert report["status"] == "issue"
    assert report["issue_count"] == 2
    assert report["unverified_count"] == 1
    assert finding["check_id"] == "NLMCA-007"
    assert finding["evidence"]["failure_category"] == "authentication_failure"
    assert finding["evidence"]["http_status"] == 401
    assert reader.refresh_count == 4
    assert reader.calls == [
        ("get_course_structure", COURSE_KEY),
        ("get_files", GALLERY),
        ("get_files", GALLERY),
        ("get_files", GALLERY),
        ("get_files", GALLERY),
        ("get_files", GALLERY),
    ]
    assert [check["status"] for check in report["target_sections"][0]["checks"]] == ["pass", "issue", "pass", "issue"]


def test_course_and_section_urls_resolve_to_the_unique_configured_tenant(tmp_path):
    config_path = tenant_config(
        tmp_path,
        {
            "uat": {"studio_base": FakeStudioReader.studio_base},
            "prod": {"studio_base": "https://studio.example.prod"},
        },
    )
    expected_urls = [
        f"https://studio.example.test/course/{COURSE_KEY}",
        f"https://studio.example.test/container/{VERTICAL}",
        f"http://studio.example.test/course/{COURSE_KEY}",
    ]

    for index, studio_url in enumerate(expected_urls):
        reader = FakeStudioReader()
        reader_factory = FakeReaderFactory(reader)
        report = auditor.run_audit_request(
            studio_url=studio_url,
            tenant_config_path=config_path,
            reader_factory=reader_factory,
            working_directory=tmp_path / str(index),
            audited_at="2026-10-28T01:02:03Z",
        )

        assert report["status"] == "pass"
        assert report["tenant"] == "uat"
        assert report["course_id"] == COURSE_KEY
        assert reader_factory.calls == [{
            "studio_base": FakeStudioReader.studio_base,
            "courses_base": "https://courses.example.test",
            "course_id": COURSE_KEY,
            "environ": None,
        }]


def test_unsupported_ambiguous_or_mixed_inputs_fail_before_reader_creation(tmp_path):
    reader_factory = FakeReaderFactory(FakeStudioReader())
    config_path = tenant_config(tmp_path)
    invalid_inputs = [
        {"studio_url": f"https://unknown.example/course/{COURSE_KEY}"},
        {"studio_url": f"https://studio.example.test/course/1040663"},
        {"studio_url": f"https://studio.example.test/course/{COURSE_KEY}/unsupported"},
        {"studio_url": f"https://user:password@studio.example.test/course/{COURSE_KEY}"},
        {"studio_url": f"https://studio.example.test/unsupported/{COURSE_KEY}"},
        {"studio_url": "https://studio.example.test/container/block-v1:ORG+COURSE+RUN+type@sequential+block@unit"},
        {"tenant": "uat", "course_id": COURSE_KEY, "studio_url": f"https://studio.example.test/course/{COURSE_KEY}"},
        {"tenant": "uat", "course_id": "1040663"},
        {"course_id": COURSE_KEY},
        {"tenant": "uat"},
    ]

    for values in invalid_inputs:
        with pytest.raises(auditor.AuditError):
            auditor.run_audit_request(
                **values,
                tenant_config_path=config_path,
                reader_factory=reader_factory,
                working_directory=tmp_path / "rejected",
            )
    assert reader_factory.calls == []

    ambiguous_config = tenant_config(
        tmp_path / "ambiguous",
        {
            "uat": {"studio_base": FakeStudioReader.studio_base},
            "uat-copy": {"studio_base": FakeStudioReader.studio_base},
        },
    )
    with pytest.raises(auditor.AuditError):
        auditor.run_audit_request(
            studio_url=f"https://studio.example.test/course/{COURSE_KEY}",
            tenant_config_path=ambiguous_config,
            reader_factory=reader_factory,
            working_directory=tmp_path / "ambiguous-out",
        )
    assert reader_factory.calls == []


def test_explicit_pair_and_url_resolve_to_the_same_course_and_studio_links(tmp_path):
    config_path = tenant_config(tmp_path)
    explicit_reader = FakeStudioReader()
    explicit_report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(explicit_reader),
        working_directory=tmp_path / "explicit",
        audited_at="2026-10-28T01:02:03Z",
    )
    url_reader = FakeStudioReader()
    url_report = auditor.run_audit_request(
        studio_url=f"https://studio.example.test/course/{COURSE_KEY}",
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(url_reader),
        working_directory=tmp_path / "url",
        audited_at="2026-10-28T01:02:03Z",
    )

    assert (explicit_report["tenant"], explicit_report["course_id"]) == (
        url_report["tenant"], url_report["course_id"]
    )
    assert explicit_report["target_sections"][0]["studio_url"] == url_report["target_sections"][0]["studio_url"]


@pytest.mark.parametrize("auth_mode", ["account", "cookie"])
def test_complete_authenticated_run_uses_configured_host_and_csrf_for_gallery_reads(tmp_path, auth_mode):
    config_path = tenant_config(
        tmp_path,
        {
            "uat": {
                "studio_base": FakeStudioReader.studio_base,
                "courses_base": "https://courses.example.test",
            }
        },
    )
    sessions = []

    def reader_factory(**kwargs):
        return auditor.StudioApiReader(
            **kwargs,
            session_factory=lambda: FakeHttpSession(sessions=sessions, course_tree=course_tree_with_one_target()),
        )

    if auth_mode == "account":
        environment = {"FIRA_SAAS_OP_ACCOUNT": "account-secret", "FIRA_SAAS_OP_PASSWORD": "password-secret"}
        expected_csrf = "landing-csrf-secret"
    else:
        environment = {
            "FIRA_SAAS_OP_COOKIE": "sessionid=cookie-secret",
            "FIRA_SAAS_OP_CSRF_TOKEN": "cookie-csrf-secret",
        }
        expected_csrf = "cookie-csrf-secret"

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=reader_factory,
        environ=environment,
        working_directory=tmp_path / auth_mode,
        audited_at="2026-10-28T01:02:03Z",
    )

    assert report["status"] == "pass"
    calls = [call for session in sessions for call in session.calls]
    tree_reads = [call for call in calls if call[0] == "GET" and call[3].get("params") == {"format": "concise"}]
    assert len(tree_reads) == 1
    gallery_reads = [call for call in calls if call[0] == "POST" and "/handler/" in call[2]]
    assert {call[2].rsplit("/", 1)[-1] for call in gallery_reads} == {"get_files", "get_play_config", "get_audios"}
    assert all(call[1] == "studio.example.test" for call in tree_reads + gallery_reads)
    assert all(call[3]["headers"]["X-CSRFToken"] == expected_csrf for call in gallery_reads)
    assert all(call[3].get("allow_redirects") is False for call in tree_reads + gallery_reads)
    report_text = json.dumps(report, ensure_ascii=False)
    for secret in ("account-secret", "password-secret", "cookie-secret", "cookie-csrf-secret", "landing-csrf-secret", "jwt-secret"):
        assert secret not in report_text


def test_missing_authentication_writes_global_unverified_report_without_studio_reads(tmp_path):
    config_path = tenant_config(tmp_path)
    sessions = []

    def reader_factory(**kwargs):
        return auditor.StudioApiReader(
            **kwargs,
            session_factory=lambda: FakeHttpSession(sessions=sessions, course_tree=course_tree_with_one_target()),
        )

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=reader_factory,
        environ={},
        working_directory=tmp_path / "missing-auth",
    )

    assert report["status"] == "unverified"
    assert report["course_findings"][0]["evidence"]["failure_category"] == "authentication_failure"
    assert report["target_sections"] == []
    assert sessions[0].calls == []
    output_dir = tmp_path / "missing-auth" / "auditor" / COURSE_KEY.replace(":", "_")
    assert (output_dir / "report.json").exists()
    assert not (output_dir / "course-structure.json").exists()


def test_login_session_refresh_reuses_the_same_course_snapshot_after_auth_handler_error(tmp_path):
    config_path = tenant_config(
        tmp_path,
        {
            "uat": {
                "studio_base": FakeStudioReader.studio_base,
                "courses_base": "https://courses.example.test",
            }
        },
    )
    sessions = []
    auth_failures = [1]

    def reader_factory(**kwargs):
        return auditor.StudioApiReader(
            **kwargs,
            session_factory=lambda: FakeHttpSession(
                sessions=sessions,
                course_tree=course_tree_with_one_target(),
                auth_failures=auth_failures,
            ),
        )

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=reader_factory,
        environ={"FIRA_SAAS_OP_ACCOUNT": "account-secret", "FIRA_SAAS_OP_PASSWORD": "password-secret"},
        working_directory=tmp_path / "auth-refresh",
        audited_at="2026-10-28T01:02:03Z",
    )

    calls = [call for session in sessions for call in session.calls]
    login_calls = [call for call in calls if call[0] == "POST" and call[2] == "/openapi/v1/auth/login/"]
    tree_reads = [call for call in calls if call[0] == "GET" and call[3].get("params") == {"format": "concise"}]
    assert report["status"] == "pass"
    assert len(sessions) == 2
    assert len(login_calls) == 2
    assert len(tree_reads) == 1
