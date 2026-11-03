import copy
import io
import json
from pathlib import Path
from urllib.parse import urlparse
import pytest

from scripts import nlm_course_auditor as auditor
from scripts.nlm_course_auditor import StudioReadError, audit_course


COURSE_KEY = "course-v1:FIRAx+1040663+20261018"
VERTICAL = "block-v1:FIRAx+1040663+20261018+type@vertical+block@lesson1"
GALLERY = "block-v1:FIRAx+1040663+20261018+type@imagesgallery+block@gallery1"
GALLERY_EXTRA = "block-v1:FIRAx+1040663+20261018+type@imagesgallery+block@gallery1b"


class FakeStudioReader:
    studio_base = "https://studio.example.test"

    def __init__(
        self,
        *,
        course_tree=None,
        publication_tree=None,
        publication_error=None,
        gallery_overrides=None,
        course_error=None,
    ):
        self.course_tree = course_tree or course_tree_with_one_target()
        self.publication_tree = publication_tree if publication_tree is not None else self.course_tree
        self.publication_error = publication_error
        self.publication_calls = []
        self.gallery_overrides = gallery_overrides or {}
        self.course_error = course_error
        self.calls = []

    def get_course_structure(self, course_id):
        self.calls.append(("get_course_structure", course_id))
        if self.course_error:
            raise self.course_error
        return self.course_tree

    def get_course_publication_structure(self, course_id):
        self.publication_calls.append(course_id)
        if self.publication_error:
            raise self.publication_error
        return self.publication_tree

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
            "play_config", {"play_config": {"time_config": [
                {"page": 1, "subtitle": "Saved subtitle for page one"},
                {"page": 2, "subtitle": "Saved subtitle for page two"},
            ]}}
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


class MutableFakeStudioClient(FakeStudioReader):
    def __init__(
        self,
        play_config,
        *,
        play_configs=None,
        unreadable_on_repair=(),
        repair_config_overrides=None,
        **kwargs,
    ):
        super().__init__(**kwargs)
        self.play_config = copy.deepcopy(play_config)
        self.play_configs = {GALLERY: self.play_config}
        self.play_configs.update(copy.deepcopy(play_configs or {}))
        self.unreadable_on_repair = set(unreadable_on_repair)
        self.repair_config_overrides = copy.deepcopy(repair_config_overrides or {})
        self.play_config_reads = {}
        self.calls = []
        self.updated_configs = []
        self.update_response = {"ok": True}
        self.publish_response = {"ok": True}

    def _update_sequential_state(self, locator, **values):
        def contains(node):
            if node.get("id") == locator:
                return True
            child_info = node.get("child_info")
            children = child_info.get("children", []) if isinstance(child_info, dict) else []
            return any(contains(child) for child in children if isinstance(child, dict))

        def visit(node):
            child_info = node.get("child_info")
            children = child_info.get("children", []) if isinstance(child_info, dict) else []
            if node.get("category") == "sequential" and contains(node):
                node.update(values)
            for child in children:
                if isinstance(child, dict):
                    visit(child)

        visit(self.course_tree)

    def get_course_structure(self, course_id):
        self.calls.append(("get_course_structure", course_id))
        return self.course_tree

    def get_course_publication_structure(self, course_id):
        self.calls.append(("get_course_publication_structure", course_id))
        return self.publication_tree

    def get_files(self, gallery_locator):
        self.calls.append(("get_files", gallery_locator))
        return self._gallery_response("files", {
            "files": [{"id": "asset-1"}, {"id": "asset-2"}],
            "total_count": 2,
        })

    def get_play_config(self, gallery_locator):
        self.calls.append(("get_play_config", gallery_locator))
        read_count = self.play_config_reads.get(gallery_locator, 0) + 1
        self.play_config_reads[gallery_locator] = read_count
        if gallery_locator in self.unreadable_on_repair and read_count >= 2:
            return {"play_config": {"time_config": "unreadable"}}
        if gallery_locator in self.repair_config_overrides and read_count >= 2:
            return copy.deepcopy(self.repair_config_overrides[gallery_locator])
        return copy.deepcopy(self.play_configs.get(gallery_locator, self.play_config))

    def get_audios(self, gallery_locator):
        self.calls.append(("get_audios", gallery_locator))
        return self._gallery_response("audios", {"audios": [{"id": "audio-1"}]})

    def update_play_config(self, gallery_locator, play_config):
        self.calls.append(("update_play_config", gallery_locator))
        if isinstance(self.update_response, dict) and self.update_response.get("success", True) is not False:
            self.play_configs[gallery_locator] = {"play_config": copy.deepcopy(play_config)}
            self._update_sequential_state(gallery_locator, has_changes=True)
        self.updated_configs.append(copy.deepcopy(play_config))
        return copy.deepcopy(self.update_response)

    def publish_vertical(self, vertical_locator):
        self.calls.append(("publish_vertical", vertical_locator))
        if isinstance(self.publish_response, dict) and self.publish_response.get("success", True) is not False:
            self._update_sequential_state(vertical_locator, published=True, has_changes=False, visibility_state="live")
        return copy.deepcopy(self.publish_response)


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
    def __init__(self, *, sessions, course_tree, publication_tree=None, auth_failures=None, play_config=None):
        self.headers = {}
        self.cookies = FakeCookieJar()
        self.sessions = sessions
        self.course_tree = course_tree
        self.publication_tree = publication_tree if publication_tree is not None else course_tree
        self.auth_failures = auth_failures if auth_failures is not None else [0]
        self.play_config = play_config or {"play_config": {"time_config": [
            {"page": 1, "subtitle": "Saved subtitle for page one"},
            {"page": 2, "subtitle": "Saved subtitle for page two"},
        ]}}
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
            return FakeHttpResponse(copy.deepcopy(self.play_config))
        if parsed.path.endswith("/handler/get_audios"):
            return FakeHttpResponse({"audios": [{"id": "audio-secret"}]})
        if parsed.path.endswith("/handler/update_play_config"):
            self.play_config = copy.deepcopy(kwargs.get("json"))
            return FakeHttpResponse({"ok": True})
        if parsed.path == f"/xblock/{VERTICAL}":
            return FakeHttpResponse({"ok": True})
        raise AssertionError(f"Unexpected POST path: {parsed.path}")

    def get(self, url, **kwargs):
        parsed = urlparse(url)
        params = kwargs.get("params")
        self.calls.append(("GET", parsed.netloc, parsed.path, kwargs))
        if (
            parsed.path == f"/course/{COURSE_KEY}"
            and kwargs.get("headers", {}).get("Accept") == "application/json, text/plain, */*"
        ):
            if params == {"format": "concise"}:
                return FakeHttpResponse(self.course_tree)
            if params is None:
                return FakeHttpResponse(self.publication_tree)
            raise AssertionError(f"Unexpected course query parameters: {params}")
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
                                    "published": True,
                                    "has_changes": False,
                                    "visibility_state": "live",
                                    "released_to_students": True,
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


def course_tree_with_multiple_repair_targets():
    tree = course_tree_with_one_target()
    chapter = tree["child_info"]["children"][0]
    first_sequential = chapter["child_info"]["children"][0]
    first_vertical = first_sequential["child_info"]["children"][0]
    first_vertical["child_info"]["children"].insert(1, {
        "id": GALLERY_EXTRA,
        "category": "imagesgallery",
        "display_name": "补充讲解",
    })

    second_sequential = copy.deepcopy(first_sequential)
    second_sequential["id"] = second_sequential["id"].replace("block@section1", "block@section2")
    second_sequential["display_name"] = "第二节"
    second_vertical = second_sequential["child_info"]["children"][0]
    second_vertical["id"] = second_vertical["id"].replace("block@lesson1", "block@lesson2")
    second_vertical["child_info"]["children"].pop(1)
    second_gallery = second_vertical["child_info"]["children"][0]
    second_gallery["id"] = second_gallery["id"].replace("block@gallery1", "block@gallery2")
    second_summary = second_vertical["child_info"]["children"][1]
    second_summary["id"] = second_summary["id"].replace("block@summary", "block@summary2")
    second_non_target = second_sequential["child_info"]["children"][1]
    second_non_target["id"] = second_non_target["id"].replace("block@not-target", "block@not-target2")
    chapter["child_info"]["children"].append(second_sequential)
    return tree, second_vertical["id"], second_gallery["id"]


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
    assert reader.publication_calls == [COURSE_KEY]
    assert report["schema_version"] == "1.2.0"
    assert report["tenant"] == "uat"
    assert report["course_id"] == COURSE_KEY
    assert report["status"] == "pass"
    assert report["pass_count"] == 11
    assert report["issue_count"] == 0
    assert report["unverified_count"] == 0
    assert report["course_findings"] == []
    assert len(report["target_sections"]) == 1

    section = report["target_sections"][0]
    assert section["name"] == " 赋能内容 "
    assert section["locator"] == VERTICAL
    assert section["studio_url"] == f"{reader.studio_base}/container/{VERTICAL}"
    assert report["sequential_sections"][0]["checks"][0]["status"] == "pass"
    assert "released_to_students" not in report["sequential_sections"][0]["checks"][0]["actual"]
    assert [check["check_id"] for check in section["checks"]] == [
        "NLMCA-001",
        "NLMCA-002",
        "NLMCA-003",
        "NLMCA-004",
        "NLMCA-005",
        "NLMCA-010",
        "NLMCA-011",
        "NLMCA-012",
        "NLMCA-008",
        "NLMCA-009",
    ]
    assert all(check["status"] == "pass" for check in section["checks"])
    assert section["checks"][2]["actual"] == {"valid_file_count": 2, "total_count": 2}
    assert section["checks"][3]["actual"] == {"time_config_count": 2, "valid_file_count": 2}
    assert "operator_hint" not in section["checks"][3]
    assert section["checks"][4]["actual"] == {"audio_binding_count": 1}
    assert section["checks"][5]["actual"] == {"affected_page_count": 0, "marker_count": 0}
    assert section["checks"][6]["actual"] == {"eligible_page_count": 2, "affected_page_count": 0}
    assert section["checks"][7]["actual"] == {
        "eligible_page_count": 2,
        "affected_page_count": 0,
        "unrecognized_bold_count": 0,
    }
    assert section["checks"][8]["actual"] == {
        "direct_child_count": 2,
        "imagesgallery_positions": [1],
        "first_child": {"position": 1, "category": "imagesgallery", "name": "课程讲解"},
    }
    assert section["checks"][9]["actual"] == {
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


def test_audit_reports_publication_state_for_all_sequential_sections(tmp_path):
    course_tree = course_tree_with_one_target()
    first_sequential = course_tree["child_info"]["children"][0]["child_info"]["children"][0]
    first_sequential.update({
        "published": True,
        "has_changes": True,
        "visibility_state": "needs_attention",
        "released_to_students": False,
    })
    second_sequential = {
        "id": "block-v1:FIRAx+1040663+20261018+type@sequential+block@section2",
        "category": "sequential",
        "display_name": "第二节",
        "published": False,
        "has_changes": False,
        "released_to_students": True,
        "child_info": {"children": []},
    }
    course_tree["child_info"]["children"][0]["child_info"]["children"].append(second_sequential)
    publication_tree = copy.deepcopy(course_tree)
    publication_tree["child_info"]["children"][0]["child_info"]["children"].reverse()
    reader = FakeStudioReader(course_tree=course_tree, publication_tree=publication_tree)

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
    )

    assert report["status"] == "issue"
    assert report["sequential_sections"] == [
        {
            "name": "第一节",
            "locator": "block-v1:FIRAx+1040663+20261018+type@sequential+block@section1",
            "studio_url": f"{reader.studio_base}/container/block-v1:FIRAx+1040663+20261018+type@sequential+block@section1",
            "checks": [
                {
                    "check_id": "NLMCA-013",
                    "category": "小节发布状态",
                    "name": "Sequential subsection has no unpublished edits",
                    "status": "issue",
                    "expected": {"published": True, "has_changes": False},
                    "actual": {
                        "published": True,
                        "has_changes": True,
                        "visibility_state": "needs_attention",
                    },
                    "evidence": {
                        "failure_categories": ["unpublished_changes"],
                        "summary": "The current subsection has edits that are not published.",
                    },
                }
            ],
        },
        {
            "name": "第二节",
            "locator": "block-v1:FIRAx+1040663+20261018+type@sequential+block@section2",
            "studio_url": f"{reader.studio_base}/container/block-v1:FIRAx+1040663+20261018+type@sequential+block@section2",
            "checks": [
                {
                    "check_id": "NLMCA-013",
                    "category": "小节发布状态",
                    "name": "Sequential subsection has no unpublished edits",
                    "status": "issue",
                    "expected": {"published": True, "has_changes": False},
                    "actual": {"published": False, "has_changes": False},
                    "evidence": {
                        "failure_categories": ["not_published"],
                        "summary": "The subsection has no published version.",
                    },
                }
            ],
        },
    ]
    assert report["issue_count"] == 2
    assert all("released_to_students" not in section["checks"][0]["actual"] for section in report["sequential_sections"])


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("published", None),
        ("published", "true"),
        ("has_changes", None),
        ("has_changes", "false"),
        ("visibility_state", 1),
    ],
)
def test_missing_or_invalid_publication_state_is_unverified(tmp_path, field, value):
    course_tree = course_tree_with_one_target()
    sequential = course_tree["child_info"]["children"][0]["child_info"]["children"][0]
    sequential[field] = value

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=FakeStudioReader(course_tree=course_tree),
        working_directory=tmp_path,
    )

    check = report["sequential_sections"][0]["checks"][0]
    assert check["status"] == "unverified"
    assert check["evidence"]["failure_category"] == "invalid_publication_state"
    assert report["status"] == "unverified"
    assert report["unverified_count"] == 1


def test_publication_metadata_read_failure_does_not_discard_structure_audit(tmp_path):
    reader = FakeStudioReader(
        publication_error=StudioReadError(
            "course_publication_structure",
            "private response details",
            503,
        )
    )

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
    )

    publication_check = report["sequential_sections"][0]["checks"][0]
    assert report["status"] == "unverified"
    assert report["target_sections"]
    assert all(check["status"] == "pass" for check in report["target_sections"][0]["checks"])
    assert publication_check["status"] == "unverified"
    assert publication_check["actual"] == {}
    assert publication_check["evidence"] == {
        "failure_stage": "course_publication_structure",
        "failure_category": "read_failure",
        "summary": "Studio course_publication_structure read failed with HTTP 503.",
        "http_status": 503,
    }
    assert "private response details" not in json.dumps(report, ensure_ascii=False)


def test_attention_visibility_is_an_unpublished_changes_issue_even_when_has_changes_is_false(tmp_path):
    course_tree = course_tree_with_one_target()
    sequential = course_tree["child_info"]["children"][0]["child_info"]["children"][0]
    sequential.update({"published": True, "has_changes": False, "visibility_state": "needs_attention"})

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=FakeStudioReader(course_tree=course_tree),
        working_directory=tmp_path,
    )

    check = report["sequential_sections"][0]["checks"][0]
    assert check["status"] == "issue"
    assert check["evidence"]["failure_categories"] == ["unpublished_changes"]


def test_time_config_exceeding_valid_files_reports_operator_hint_in_all_outputs(tmp_path, capsys):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [
            {"page": 1, "subtitle": "Saved subtitle for page one"},
            {"page": 2, "subtitle": "Saved subtitle for page two"},
            {"page": 3, "subtitle": "Saved subtitle for page three"},
        ]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-004")
    assert report["schema_version"] == "1.2.0"
    assert check["status"] == "issue"
    assert check["actual"] == {"time_config_count": 3, "valid_file_count": 2}
    assert check["evidence"]["time_config_count"] == 3
    assert check["operator_hint"] == "大概率是 imagegallery 有几页图片未成功上传。"
    assert report["status"] == "issue"
    assert report["issue_count"] == 1
    output_dir = tmp_path / "auditor" / COURSE_KEY.replace(":", "_")
    assert json.loads((output_dir / "report.json").read_text(encoding="utf-8")) == report
    assert capsys.readouterr().out.strip() == json.dumps(report, ensure_ascii=False, indent=2)


def test_time_config_fewer_than_valid_files_remains_factual_issue_without_operator_hint(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [
            {"page": 1, "subtitle": "Saved subtitle for page one"},
        ]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-004")
    assert check["status"] == "issue"
    assert check["actual"] == {"time_config_count": 1, "valid_file_count": 2}
    assert check["evidence"]["time_config_count"] == 1
    assert "operator_hint" not in check
    assert report["status"] == "issue"
    assert report["issue_count"] == 1
    assert report["unverified_count"] == 0


def test_non_list_time_config_reports_audio_subtitle_operator_hint(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": "unreadable"}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {item["check_id"]: item for item in report["target_sections"][0]["checks"]}
    assert checks["NLMCA-004"]["status"] == "unverified"
    assert checks["NLMCA-004"]["actual"] is None
    assert checks["NLMCA-004"]["evidence"]["failure_category"] == "invalid_response"
    assert checks["NLMCA-004"]["operator_hint"] == "大概率是 imagegallery 的音频字幕未成功上传。"
    assert checks["NLMCA-010"]["status"] == "unverified"
    assert "operator_hint" not in checks["NLMCA-010"]
    assert report["unverified_count"] == 2


def test_saved_gallery_blockquote_prefixes_report_page_counts_and_section_link(tmp_path):
    section_url = f"{FakeStudioReader.studio_base}/container/{VERTICAL}"
    raw_subtitle = "> > confidential lesson text\n   > > > another private sentence\nA > B"
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {
            "play_config": {
                "time_config": [
                    {"page": 1, "subtitle": raw_subtitle},
                    {"page": 2, "subtitle": "&gt; &gt quoted text"},
                    {"page": 3, "subtitle": "   &gt; > &#62; &#x3E; quoted text"},
                    {"page": 4, "subtitle": "    &gt; indented text\nA &gt; B\n&amp;gt;"},
                ]
            }
        },
        "files": {
            "files": [{"id": f"asset-{index}"} for index in range(1, 5)],
            "total_count": 4,
        },
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    section = report["target_sections"][0]
    check = next(item for item in section["checks"] if item["check_id"] == "NLMCA-010")
    report_text = json.dumps(report, ensure_ascii=False)
    assert check["status"] == "issue"
    assert check["evidence"]["affected_pages"] == [
        {"page": 1, "marker_count": 5},
        {"page": 2, "marker_count": 2},
        {"page": 3, "marker_count": 4},
    ]
    assert check["evidence"]["total_marker_count"] == 11
    assert f"[Open Studio section]({section_url})" in check["evidence"]["summary"]
    assert section["studio_url"] == section_url
    section_checks = {item["check_id"]: item for item in section["checks"]}
    assert [section_checks[check_id]["status"] for check_id in (
        "NLMCA-003", "NLMCA-004", "NLMCA-005"
    )] == ["pass", "pass", "pass"]
    assert raw_subtitle not in report_text
    assert "confidential lesson text" not in report_text
    assert json.loads(
        (tmp_path / "auditor" / COURSE_KEY.replace(":", "_") / "report.json").read_text(encoding="utf-8")
    ) == report


def test_readable_saved_gallery_subtitles_without_prefixes_pass(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {
            "play_config": {
                "time_config": [
                    {"page": 1, "subtitle": "A > B"},
                    {"page": 2, "subtitle": "A > B\n    > four-space indentation\n\t> tab indentation"},
                ]
            }
        }
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-010")
    assert check["status"] == "pass"
    assert check["actual"] == {"affected_page_count": 0, "marker_count": 0}
    assert check["evidence"]["affected_pages"] == []


def test_saved_subtitle_bold_candidates_use_commonmark_and_keep_report_private(tmp_path):
    section_url = f"{FakeStudioReader.studio_base}/container/{VERTICAL}"
    raw_subtitles = [
        "** 未识别 **",
        "正文** 未闭合",
        "中文前文**加粗重点**，中文后文以及****嵌套格式****",
        r"字面量 \*\*，行内 `**code**` 和 `**\`，以及：" "\n```md\n**fenced code**\n```",
        "one ** two ** three",
    ]
    reader = FakeStudioReader(gallery_overrides={
        "files": {
            "files": [{"id": f"asset-{index}"} for index in range(1, len(raw_subtitles) + 1)],
            "total_count": len(raw_subtitles),
        },
        "play_config": {"play_config": {"time_config": [
            {"page": page, "subtitle": subtitle}
            for page, subtitle in zip((3, 4, 5, 6, 7), raw_subtitles)
        ]}},
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    section = report["target_sections"][0]
    checks = {item["check_id"]: item for item in section["checks"]}
    check = checks["NLMCA-012"]
    assert check["status"] == "issue"
    assert check["actual"] == {
        "eligible_page_count": 5,
        "affected_page_count": 3,
        "unrecognized_bold_count": 5,
    }
    assert check["evidence"]["gallery_locator"] == GALLERY
    assert check["evidence"]["affected_pages"] == [
        {"page": 3, "unrecognized_bold_count": 2},
        {"page": 4, "unrecognized_bold_count": 1},
        {"page": 7, "unrecognized_bold_count": 2},
    ]
    assert f"[Open Studio section]({section_url})" in check["evidence"]["summary"]
    assert checks["NLMCA-010"]["status"] == "pass"
    assert [checks[check_id]["status"] for check_id in ("NLMCA-003", "NLMCA-004", "NLMCA-005")] == [
        "pass", "pass", "pass"
    ]
    saved_report = json.loads(
        (tmp_path / "auditor" / COURSE_KEY.replace(":", "_") / "report.json").read_text(encoding="utf-8")
    )
    report_text = json.dumps(saved_report, ensure_ascii=False)
    assert saved_report == report
    assert all(subtitle not in report_text for subtitle in raw_subtitles)
    assert "未识别" not in report_text
    assert "fenced code" not in report_text


def test_paragraph_before_markdown_heading_is_reported_without_subtitle_text(tmp_path, capsys):
    raw_subtitle = "\n\nPrivate opening explanation.\n\n# Page title\nDetails follow."
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [{"page": 7, "subtitle": raw_subtitle}]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(
        item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-011"
    )
    assert check["status"] == "issue"
    assert check["actual"] == {"eligible_page_count": 1, "affected_page_count": 1}
    assert check["evidence"]["affected_pages"] == [
        {"page": 7, "opening_block": "paragraph", "later_heading_count": 1}
    ]
    assert check["operator_hint"] == "大概率是 AI 段落划分不准确。"
    assert "operator_hint" not in check["evidence"]
    report_text = json.dumps(report, ensure_ascii=False)
    assert raw_subtitle not in report_text
    assert "Private opening explanation" not in report_text
    output_dir = tmp_path / "auditor" / COURSE_KEY.replace(":", "_")
    assert json.loads((output_dir / "report.json").read_text(encoding="utf-8")) == report
    assert capsys.readouterr().out.strip() == json.dumps(report, ensure_ascii=False, indent=2)


def test_paragraph_before_setext_heading_is_reported(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [
            {"page": 4, "subtitle": "Private introduction.\n\nPage title\n-"},
        ]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(
        item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-011"
    )
    assert check["status"] == "issue"
    assert check["evidence"]["affected_pages"] == [
        {"page": 4, "opening_block": "paragraph", "later_heading_count": 1}
    ]


def test_subtitle_checks_fall_back_to_time_config_index_for_invalid_page_numbers(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [
            {"page": True, "subtitle": "> quoted text"},
            {"subtitle": "> another quote"},
            {"page": 0, "subtitle": "Private body.\n\n# Later heading"},
            {"page": False, "subtitle": "Private body.\n\n# Later heading"},
        ]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {item["check_id"]: item for item in report["target_sections"][0]["checks"]}
    assert [item["page"] for item in checks["NLMCA-010"]["evidence"]["affected_pages"]] == [1, 2]
    assert [item["page"] for item in checks["NLMCA-011"]["evidence"]["affected_pages"]] == [3, 4]


@pytest.mark.parametrize("subtitle", [
    "# Page title\n\n## A later heading\n\n### Another heading",
    "- A list item\n\n# A later heading",
    "> A quote\n\n# A later heading",
    "| Name | Value |\n| --- | --- |\n| one | two |\n\n# A later heading",
    "```md\n# Code is not a heading\n```\n\n# A later heading",
    "    # Indented code is not a heading\n\n# A later heading",
    "A paragraph without a later heading.",
])
def test_first_block_must_be_a_paragraph_before_a_markdown_heading_can_trigger(tmp_path, subtitle):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [{"page": 5, "subtitle": subtitle}]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(
        item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-011"
    )
    assert check["status"] == "pass"
    assert check["actual"] == {"eligible_page_count": 1, "affected_page_count": 0}
    assert "operator_hint" not in check


def test_bold_only_opening_text_is_a_paragraph_before_a_real_heading(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [
            {"page": 5, "subtitle": "**A bold phrase is still a paragraph.**\n\n# Actual heading"},
        ]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(
        item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-011"
    )
    assert check["status"] == "issue"
    assert check["actual"] == {"eligible_page_count": 1, "affected_page_count": 1}


def test_each_affected_page_is_reported_once_with_all_counts(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "files": {"files": [{"id": "asset-1"}, {"id": "asset-2"}, {"id": "asset-3"}], "total_count": 3},
        "play_config": {"play_config": {"time_config": [
            {"page": 2, "subtitle": "Private page two body.\n\n# First title\n\n## Second title"},
            {"page": 8, "subtitle": "Private page eight body.\n\nPage title\n==="},
            {"page": 9, "subtitle": "A page with no heading."},
        ]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    check = next(
        item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-011"
    )
    assert check["status"] == "issue"
    assert check["actual"] == {"eligible_page_count": 3, "affected_page_count": 2}
    assert check["evidence"]["affected_pages"] == [
        {"page": 2, "opening_block": "paragraph", "later_heading_count": 2},
        {"page": 8, "opening_block": "paragraph", "later_heading_count": 1},
    ]
    assert "page 2, 8" in check["evidence"]["summary"]
    assert check["operator_hint"] == "大概率是 AI 段落划分不准确。"
    assert report["issue_count"] == 1
    report_text = json.dumps(report, ensure_ascii=False)
    assert "Private page two body" not in report_text
    assert "Private page eight body" not in report_text


@pytest.mark.parametrize(("time_config", "eligible_page_count"), [
    ([], 0),
    ([{"page": 1, "subtitle": ""}], 0),
    ([{"page": 1, "subtitle": "   \n"}], 0),
    ([{"page": 1, "subtitle": "private lesson text"}, {"page": 2}], 1),
    ([{"page": 1, "subtitle": "private lesson text"}, "malformed entry"], 1),
    ([{"page": 1, "subtitle": 17}], 0),
])
def test_missing_or_malformed_saved_subtitles_are_unverified_without_stopping_gallery_checks(
    tmp_path, time_config, eligible_page_count,
):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": time_config}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {item["check_id"]: item for item in report["target_sections"][0]["checks"]}
    assert checks["NLMCA-010"]["status"] == "unverified"
    assert checks["NLMCA-010"]["evidence"]["failure_category"] == "invalid_response"
    assert checks["NLMCA-011"]["status"] == "pass"
    assert checks["NLMCA-011"]["actual"] == {
        "eligible_page_count": eligible_page_count,
        "affected_page_count": 0,
    }
    assert checks["NLMCA-012"]["status"] == "pass"
    assert checks["NLMCA-012"]["actual"] == {
        "eligible_page_count": eligible_page_count,
        "affected_page_count": 0,
        "unrecognized_bold_count": 0,
    }
    assert checks["NLMCA-003"]["status"] == "pass"
    assert checks["NLMCA-005"]["status"] == "pass"
    assert "operator_hint" not in checks["NLMCA-004"]
    assert "private lesson text" not in json.dumps(report, ensure_ascii=False)


def test_unreadable_saved_subtitles_are_unverified_and_audio_binding_check_continues(tmp_path):
    private_error = "get_play_config returned a private subtitle payload"
    reader = FakeStudioReader(gallery_overrides={
        "play_config": StudioReadError("get_play_config", private_error, 503)
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {item["check_id"]: item for item in report["target_sections"][0]["checks"]}
    assert checks["NLMCA-010"]["status"] == "unverified"
    assert checks["NLMCA-010"]["evidence"]["http_status"] == 503
    assert checks["NLMCA-011"]["status"] == "pass"
    assert checks["NLMCA-011"]["actual"]["eligible_page_count"] == 0
    assert checks["NLMCA-012"]["status"] == "pass"
    assert checks["NLMCA-012"]["actual"]["eligible_page_count"] == 0
    assert checks["NLMCA-005"]["status"] == "pass"
    assert "operator_hint" not in checks["NLMCA-004"]
    assert private_error not in json.dumps(report, ensure_ascii=False)


def test_readable_subtitles_are_checked_when_other_pages_are_malformed(tmp_path):
    reader = FakeStudioReader(gallery_overrides={
        "play_config": {"play_config": {"time_config": [
            {"page": 1, "subtitle": "  "},
            {"page": 6, "subtitle": "Private body.\n\n# Later heading"},
        ]}}
    })

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    checks = {item["check_id"]: item for item in report["target_sections"][0]["checks"]}
    assert checks["NLMCA-010"]["status"] == "unverified"
    assert checks["NLMCA-011"]["status"] == "issue"
    assert checks["NLMCA-011"]["actual"] == {"eligible_page_count": 1, "affected_page_count": 1}
    assert checks["NLMCA-011"]["evidence"]["affected_pages"] == [
        {"page": 6, "opening_block": "paragraph", "later_heading_count": 1}
    ]
    assert checks["NLMCA-012"]["status"] == "pass"
    assert checks["NLMCA-012"]["actual"]["eligible_page_count"] == 1
    assert report["status"] == "issue"
    assert report["issue_count"] == 1
    assert report["unverified_count"] == 1


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
            "play_config": {"play_config": {"time_config": [{"page": 1, "subtitle": "Saved subtitle"}]}},
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
    assert [check["status"] for check in checks[2:14]] == ["issue", "issue", "issue", "pass", "pass", "pass"] * 2
    assert [check["status"] for check in checks[14:]] == ["pass", "pass"]
    assert all("operator_hint" not in checks[index] for index in (2, 3, 6, 7))
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
    assert [check["status"] for check in section["checks"]] == ["unverified"] * 9 + ["pass"]
    assert report["status"] == "unverified"
    assert report["issue_count"] == 0
    assert report["unverified_count"] == 9
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
    assert reader.publication_calls == [COURSE_KEY]

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
    assert [check["status"] for check in checks] == ["pass", "pass", "unverified", "unverified", "pass", "pass", "pass", "pass", "pass", "pass"]
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
    assert report["pass_count"] == 7


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
    assert [check["status"] for check in checks] == ["pass", "pass", "unverified", "unverified", "unverified", "unverified", "pass", "pass", "pass", "pass"]
    assert report["status"] == "unverified"
    assert report["unverified_count"] == 4
    assert all(check["evidence"]["failure_category"] == "invalid_response" for check in checks[2:6])
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
    assert [check["status"] for check in section["checks"]] == ["unverified"] * 9 + ["pass"]
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


def test_default_run_repairs_saved_leading_markers_then_publishes_and_reaudits(tmp_path):
    config_path = tenant_config(tmp_path)
    original_config = {
        "show_subtitles": True,
        "voice_mode": "narration",
        "time_config": [
            {
                "page": 4,
                "start": 120,
                "end": 135,
                "subtitle": "  > raw marker text",
                "audio_id": "audio-page-four",
                "custom": {"keep": "page-four"},
            },
            {
                "page": 9,
                "start": 180,
                "end": 198,
                "subtitle": "&gt; entity marker\n&gt semicolonless marker",
                "audio_id": "audio-page-nine",
            },
        ],
    }
    reader = MutableFakeStudioClient({"play_config": original_config})

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(reader),
        working_directory=tmp_path / "default-repair",
        audited_at="2026-10-28T01:02:03Z",
    )

    assert reader.calls == [
        ("get_course_structure", COURSE_KEY),
        ("get_course_publication_structure", COURSE_KEY),
        ("get_files", GALLERY),
        ("get_play_config", GALLERY),
        ("get_audios", GALLERY),
        ("get_play_config", GALLERY),
        ("update_play_config", GALLERY),
        ("publish_vertical", VERTICAL),
        ("get_course_structure", COURSE_KEY),
        ("get_course_publication_structure", COURSE_KEY),
        ("get_files", GALLERY),
        ("get_play_config", GALLERY),
        ("get_audios", GALLERY),
    ]
    assert reader.updated_configs == [{
        "show_subtitles": True,
        "voice_mode": "narration",
        "time_config": [
            {
                "page": 4,
                "start": 120,
                "end": 135,
                "subtitle": "raw marker text",
                "audio_id": "audio-page-four",
                "custom": {"keep": "page-four"},
            },
            {
                "page": 9,
                "start": 180,
                "end": 198,
                "subtitle": "entity marker\nsemicolonless marker",
                "audio_id": "audio-page-nine",
            },
        ],
    }]
    assert next(check for check in report["target_sections"][0]["checks"] if check["check_id"] == "NLMCA-010")["status"] == "pass"
    assert report["repair_summary"]["mode"] == "repair"
    assert report["repair_summary"]["targets"][0]["publish_status"] == "completed"
    report_text = json.dumps(report, ensure_ascii=False)
    assert "raw marker text" not in report_text
    assert "entity marker" not in report_text


def test_audit_only_reports_leading_markers_without_any_studio_writes(tmp_path):
    config_path = tenant_config(tmp_path)
    client = MutableFakeStudioClient({"play_config": {"time_config": [
        {"page": 1, "subtitle": "> leave this untouched"},
        {"page": 2, "subtitle": "another page"},
    ]}})

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "audit-only",
        audit_only=True,
    )

    assert client.calls == [
        ("get_course_structure", COURSE_KEY),
        ("get_course_publication_structure", COURSE_KEY),
        ("get_files", GALLERY),
        ("get_play_config", GALLERY),
        ("get_audios", GALLERY),
    ]
    assert client.play_configs[GALLERY]["play_config"]["time_config"][0]["subtitle"] == "> leave this untouched"
    assert report["repair_summary"] == {"mode": "audit_only", "repair_passes": 0, "targets": []}
    assert next(check for check in report["target_sections"][0]["checks"] if check["check_id"] == "NLMCA-010")["status"] == "issue"
    assert auditor.parse_args(["--audit-only"]).audit_only is True


def test_default_run_does_not_repeat_full_audit_when_no_markers_need_repair(tmp_path):
    config_path = tenant_config(tmp_path)
    client = MutableFakeStudioClient({"play_config": {"time_config": [
        {"page": 1, "subtitle": "paragraph before a heading\n\n# heading"},
        {"page": 2, "subtitle": "also clean"},
    ]}})

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "no-repair",
    )

    assert [call[0] for call in client.calls].count("get_course_structure") == 1
    assert not any(call[0] in {"update_play_config", "publish_vertical"} for call in client.calls)
    assert report["repair_summary"] == {"mode": "repair", "repair_passes": 0, "targets": []}
    assert next(check for check in report["target_sections"][0]["checks"] if check["check_id"] == "NLMCA-011")["status"] == "issue"


def test_repair_skips_incomplete_target_groups_gallery_updates_by_vertical_and_continues(tmp_path):
    config_path = tenant_config(tmp_path)
    tree, second_vertical, second_gallery = course_tree_with_multiple_repair_targets()
    shared_config = {"play_config": {"show_subtitles": True, "time_config": [
        {"page": 1, "start": 0, "end": 5, "subtitle": "> repair this page"},
        {"page": 2, "start": 5, "end": 14, "subtitle": "leave this page"},
    ]}}
    client = MutableFakeStudioClient(
        shared_config,
        course_tree=tree,
        play_configs={GALLERY_EXTRA: shared_config, second_gallery: shared_config},
        unreadable_on_repair={second_gallery},
    )

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "independent-targets",
    )

    operations = [call[0] for call in client.calls]
    assert operations.count("get_course_structure") == 2
    assert operations.count("get_play_config") == 9
    assert [call for call in client.calls if call[0] == "update_play_config"] == [
        ("update_play_config", GALLERY),
        ("update_play_config", GALLERY_EXTRA),
    ]
    assert [call for call in client.calls if call[0] == "publish_vertical"] == [
        ("publish_vertical", VERTICAL),
    ]
    first_target, second_target = report["repair_summary"]["targets"]
    assert first_target["vertical_locator"] == VERTICAL
    assert [item["repair_status"] for item in first_target["galleries"]] == ["repaired", "repaired"]
    assert first_target["publish_status"] == "completed"
    assert second_target["vertical_locator"] == second_vertical
    assert second_target["galleries"][0]["repair_status"] == "skipped"
    assert second_target["galleries"][0]["reason"] == "play_config_unverified"
    assert second_target["publish_status"] == "not_required"
    assert next(
        check
        for section in report["target_sections"] if section["locator"] == second_vertical
        for check in section["checks"] if check["check_id"] == "NLMCA-010"
    )["status"] == "unverified"


@pytest.mark.parametrize("failed_stage", ["update", "publish"])
def test_failed_write_still_finishes_one_final_course_audit(tmp_path, failed_stage):
    config_path = tenant_config(tmp_path)
    client = MutableFakeStudioClient({"play_config": {"show_subtitles": True, "time_config": [
        {"page": 1, "start": 0, "end": 5, "subtitle": "> approved repair"},
        {"page": 2, "start": 5, "end": 14, "subtitle": "unchanged"},
    ]}})
    if failed_stage == "update":
        client.update_response = None
    else:
        client.publish_response = {"success": False}

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / failed_stage,
    )

    assert [call[0] for call in client.calls].count("get_course_structure") == 2
    target = report["repair_summary"]["targets"][0]
    assert target["galleries"][0]["repair_status"] == ("failed" if failed_stage == "update" else "repaired")
    assert target["publish_status"] == ("not_required" if failed_stage == "update" else "failed")
    assert ("publish_vertical", VERTICAL) in client.calls if failed_stage == "publish" else not any(
        call[0] == "publish_vertical" for call in client.calls
    )
    final_markers = next(
        check for check in report["target_sections"][0]["checks"] if check["check_id"] == "NLMCA-010"
    )
    assert final_markers["status"] == ("pass" if failed_stage == "publish" else "issue")
    if failed_stage == "publish":
        assert report["sequential_sections"][0]["checks"][0]["status"] == "issue"


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
    concise_tree = course_tree_with_one_target()
    publication_tree = copy.deepcopy(concise_tree)
    default_sequential = publication_tree["child_info"]["children"][0]["child_info"]["children"][0]
    default_vertical = default_sequential["child_info"]["children"][0]
    default_vertical.pop("child_info")

    def reader_factory(**kwargs):
        return auditor.StudioApiClient(
            **kwargs,
            session_factory=lambda: FakeHttpSession(
                sessions=sessions,
                course_tree=concise_tree,
                publication_tree=publication_tree,
            ),
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
    tree_reads = [
        call for call in calls
        if (
            call[0] == "GET"
            and call[2] == f"/course/{COURSE_KEY}"
            and call[3].get("headers", {}).get("Accept") == "application/json, text/plain, */*"
        )
    ]
    assert [call[3].get("params") for call in tree_reads] == [{"format": "concise"}, None]
    output_dir = tmp_path / auth_mode / "auditor" / COURSE_KEY.replace(":", "_")
    assert json.loads((output_dir / "course-structure.json").read_text(encoding="utf-8")) == concise_tree
    assert report["sequential_sections"][0]["checks"][0]["status"] == "pass"
    gallery_reads = [call for call in calls if call[0] == "POST" and "/handler/" in call[2]]
    assert {call[2].rsplit("/", 1)[-1] for call in gallery_reads} == {"get_files", "get_play_config", "get_audios"}
    assert all(call[1] == "studio.example.test" for call in tree_reads + gallery_reads)
    assert all(call[3]["headers"]["X-CSRFToken"] == expected_csrf for call in gallery_reads)
    assert all(call[3].get("allow_redirects") is False for call in tree_reads + gallery_reads)
    report_text = json.dumps(report, ensure_ascii=False)
    for secret in ("account-secret", "password-secret", "cookie-secret", "cookie-csrf-secret", "landing-csrf-secret", "jwt-secret"):
        assert secret not in report_text


def test_authenticated_repair_uses_full_play_config_and_csrf_on_update_and_vertical_publish(tmp_path, capsys):
    config_path = tenant_config(tmp_path)
    sessions = []
    original = {"play_config": {
        "show_subtitles": False,
        "voice_mode": "keep-me",
        "time_config": [
            {"page": 1, "start": 0, "end": 5, "subtitle": "> repair this", "audio_id": "audio-1"},
            {"page": 2, "start": 5, "end": 12, "subtitle": "keep this", "audio_id": "audio-2"},
        ],
    }}

    def reader_factory(**kwargs):
        return auditor.StudioApiClient(
            **kwargs,
            session_factory=lambda: FakeHttpSession(
                sessions=sessions,
                course_tree=course_tree_with_one_target(),
                play_config=original,
            ),
        )

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=reader_factory,
        environ={
            "FIRA_SAAS_OP_COOKIE": "sessionid=cookie-secret",
            "FIRA_SAAS_OP_CSRF_TOKEN": "cookie-csrf-secret",
        },
        working_directory=tmp_path / "authenticated-repair",
        output=io.StringIO(),
    )

    writes = [
        call for session in sessions for call in session.calls
        if call[0] == "POST" and (
            call[2].endswith("/handler/update_play_config") or call[2] == f"/xblock/{VERTICAL}"
        )
    ]
    assert [call[2] for call in writes] == [
        f"/xblock/{GALLERY}/handler/update_play_config",
        f"/xblock/{VERTICAL}",
    ]
    update_request, publish_request = writes
    assert update_request[3]["json"] == {"play_config": {
        "show_subtitles": False,
        "voice_mode": "keep-me",
        "time_config": [
            {"page": 1, "start": 0, "end": 5, "subtitle": "repair this", "audio_id": "audio-1"},
            {"page": 2, "start": 5, "end": 12, "subtitle": "keep this", "audio_id": "audio-2"},
        ],
    }}
    assert publish_request[3]["json"] == {"publish": "make_public"}
    for request in writes:
        assert request[3]["headers"]["X-CSRFToken"] == "cookie-csrf-secret"
        assert request[3]["headers"]["Origin"] == FakeStudioReader.studio_base
    assert publish_request[3]["headers"]["X-HTTP-Method-Override"] == "PATCH"
    assert report["repair_summary"]["targets"][0]["publish_status"] == "completed"


def test_default_run_repairs_bold_spacing_only_after_commonmark_accepts_the_result(tmp_path):
    config_path = tenant_config(tmp_path)
    original = {"play_config": {"show_subtitles": True, "time_config": [
        {"page": 3, "start": 0, "end": 12, "subtitle": "** private bold wording **"},
        {"page": 4, "start": 12, "end": 30, "subtitle": "Already **valid bold** wording"},
    ]}}
    client = MutableFakeStudioClient(original)

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "bold-repair",
        audited_at="2026-11-03T01:02:03Z",
        output=io.StringIO(),
    )

    assert client.updated_configs == [{
        "show_subtitles": True,
        "time_config": [
            {"page": 3, "start": 0, "end": 12, "subtitle": "**private bold wording**"},
            {"page": 4, "start": 12, "end": 30, "subtitle": "Already **valid bold** wording"},
        ],
    }]
    assert [call[0] for call in client.calls].count("get_course_structure") == 2
    assert [call for call in client.calls if call[0] == "publish_vertical"] == [
        ("publish_vertical", VERTICAL),
    ]
    bold_check = next(
        check for check in report["target_sections"][0]["checks"] if check["check_id"] == "NLMCA-012"
    )
    assert bold_check["status"] == "pass"
    assert report["repair_summary"]["targets"][0]["galleries"][0]["bold_repair_status"] == "repaired"
    report_text = json.dumps(report, ensure_ascii=False)
    assert "private bold wording" not in report_text


@pytest.mark.parametrize(("subtitle", "expected_reason"), [
    ("** confidential unpaired wording", "unpaired_delimiter"),
    ("** confidential first ** second ** third **", "ambiguous_bold_span"),
    ("a** confidential . **a", "normalization_unverified"),
])
def test_unpaired_ambiguous_and_still_invalid_bold_syntax_is_not_written(
    tmp_path, subtitle, expected_reason
):
    config_path = tenant_config(tmp_path)
    client = MutableFakeStudioClient({"play_config": {"show_subtitles": True, "time_config": [
        {"page": 6, "start": 0, "end": 5, "subtitle": subtitle},
        {"page": 7, "start": 5, "end": 14, "subtitle": "clean subtitle"},
    ]}})

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "bold-skip",
        output=io.StringIO(),
    )

    assert client.updated_configs == []
    assert not any(call[0] == "publish_vertical" for call in client.calls)
    assert [call[0] for call in client.calls].count("get_course_structure") == 2
    gallery_result = report["repair_summary"]["targets"][0]["galleries"][0]
    assert gallery_result["repair_status"] == "skipped"
    assert gallery_result["bold_repair_status"] == "skipped"
    expected_counts = {
        "unpaired_delimiter": 1,
        "ambiguous_bold_span": 4,
        "normalization_unverified": 2,
    }
    assert gallery_result["bold_affected_pages"] == [{
        "page": 6,
        "unrecognized_bold_count": expected_counts[expected_reason],
        "status": "skipped",
        "reason": expected_reason,
    }]
    bold_check = next(
        check for check in report["target_sections"][0]["checks"] if check["check_id"] == "NLMCA-012"
    )
    assert bold_check["status"] == "issue"
    assert subtitle not in json.dumps(report, ensure_ascii=False)


def test_leading_marker_and_bold_spacing_repairs_share_one_gallery_update(tmp_path):
    config_path = tenant_config(tmp_path)
    client = MutableFakeStudioClient({"play_config": {"show_subtitles": True, "time_config": [
        {"page": 8, "start": 0, "end": 5, "subtitle": "> ** confidential shared repair **"},
        {"page": 9, "start": 5, "end": 14, "subtitle": "unchanged wording"},
    ]}})

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "combined-repair",
        output=io.StringIO(),
    )

    assert len(client.updated_configs) == 1
    assert client.updated_configs[0]["time_config"] == [
        {"page": 8, "start": 0, "end": 5, "subtitle": "**confidential shared repair**"},
        {"page": 9, "start": 5, "end": 14, "subtitle": "unchanged wording"},
    ]
    assert [call for call in client.calls if call[0] == "publish_vertical"] == [
        ("publish_vertical", VERTICAL),
    ]
    checks = {item["check_id"]: item for item in report["target_sections"][0]["checks"]}
    assert checks["NLMCA-010"]["status"] == "pass"
    assert checks["NLMCA-012"]["status"] == "pass"
    gallery_result = report["repair_summary"]["targets"][0]["galleries"][0]
    assert gallery_result["repair_status"] == "repaired"
    assert gallery_result["bold_repair_status"] == "repaired"


def test_safe_leading_marker_repair_is_not_blocked_by_unpaired_bold_delimiter(tmp_path):
    config_path = tenant_config(tmp_path)
    original = {"play_config": {"show_subtitles": True, "time_config": [
        {"page": 1, "start": 0, "end": 5, "subtitle": "> ** confidential unpaired wording"},
        {"page": 2, "start": 5, "end": 14, "subtitle": "keep this page"},
    ]}}
    client = MutableFakeStudioClient(original)

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "independent-page-repair",
        output=io.StringIO(),
    )

    assert client.updated_configs == [{"show_subtitles": True, "time_config": [
        {"page": 1, "start": 0, "end": 5, "subtitle": "** confidential unpaired wording"},
        {"page": 2, "start": 5, "end": 14, "subtitle": "keep this page"},
    ]}]
    assert [call for call in client.calls if call[0] == "publish_vertical"] == [
        ("publish_vertical", VERTICAL),
    ]
    checks = {item["check_id"]: item for item in report["target_sections"][0]["checks"]}
    assert checks["NLMCA-010"]["status"] == "pass"
    assert checks["NLMCA-012"]["status"] == "issue"
    repair = report["repair_summary"]["targets"][0]["galleries"][0]
    assert repair["repair_status"] == "repaired"
    assert repair["bold_repair_status"] == "skipped"
    assert repair["bold_affected_pages"][0]["reason"] == "unpaired_delimiter"


@pytest.mark.parametrize("missing_field", ["show_subtitles", "page", "start", "end"])
def test_incomplete_play_config_is_skipped_before_any_write_and_reaudited(tmp_path, missing_field):
    config_path = tenant_config(tmp_path)
    complete_config = {"play_config": {"show_subtitles": True, "time_config": [
        {"page": 1, "start": 0, "end": 5, "subtitle": "> repair candidate"},
        {"page": 2, "start": 5, "end": 14, "subtitle": "untouched page"},
    ]}}
    incomplete_config = copy.deepcopy(complete_config)
    play_config = incomplete_config["play_config"]
    if missing_field == "show_subtitles":
        play_config.pop("show_subtitles")
    else:
        play_config["time_config"][0].pop(missing_field)
    client = MutableFakeStudioClient(
        complete_config,
        repair_config_overrides={GALLERY: incomplete_config},
    )

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / f"incomplete-{missing_field}",
        output=io.StringIO(),
    )

    assert client.updated_configs == []
    assert not any(call[0] == "publish_vertical" for call in client.calls)
    assert [call[0] for call in client.calls].count("get_course_structure") == 2
    gallery_result = report["repair_summary"]["targets"][0]["galleries"][0]
    assert gallery_result["repair_status"] == "skipped"
    assert gallery_result["reason"] == "play_config_unverified"
    assert next(
        item for item in report["target_sections"][0]["checks"] if item["check_id"] == "NLMCA-010"
    )["status"] == "issue"


@pytest.mark.parametrize(("invalid_field", "invalid_value"), [
    ("start", "0"),
    ("start", True),
    ("start", -1),
    ("end", "5"),
    ("end", False),
    ("end", -1),
    ("range", None),
])
def test_invalid_play_config_timing_values_are_skipped_before_any_write(tmp_path, invalid_field, invalid_value):
    config_path = tenant_config(tmp_path)
    complete_config = {"play_config": {"show_subtitles": True, "time_config": [
        {"page": 1, "start": 0, "end": 5, "subtitle": "> repair candidate"},
        {"page": 2, "start": 5, "end": 14, "subtitle": "untouched page"},
    ]}}
    invalid_config = copy.deepcopy(complete_config)
    first_page = invalid_config["play_config"]["time_config"][0]
    if invalid_field == "range":
        first_page.update({"start": 10, "end": 5})
    else:
        first_page[invalid_field] = invalid_value
    client = MutableFakeStudioClient(
        complete_config,
        repair_config_overrides={GALLERY: invalid_config},
    )

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=config_path,
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / f"invalid-{invalid_field}-{str(invalid_value).lower()}",
        output=io.StringIO(),
    )

    assert client.updated_configs == []
    assert not any(call[0] == "publish_vertical" for call in client.calls)
    assert [call[0] for call in client.calls].count("get_course_structure") == 2
    gallery_result = report["repair_summary"]["targets"][0]["galleries"][0]
    assert gallery_result["repair_status"] == "skipped"
    assert gallery_result["reason"] == "play_config_unverified"


def test_missing_authentication_writes_global_unverified_report_without_studio_reads(tmp_path):
    config_path = tenant_config(tmp_path)
    sessions = []

    def reader_factory(**kwargs):
        return auditor.StudioApiClient(
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
        return auditor.StudioApiClient(
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
    tree_reads = [
        call for call in calls
            if (
                call[0] == "GET"
                and call[2] == f"/course/{COURSE_KEY}"
                and call[3].get("headers", {}).get("Accept") == "application/json, text/plain, */*"
            )
    ]
    assert report["status"] == "pass"
    assert len(sessions) == 2
    assert len(login_calls) == 2
    assert [call[3].get("params") for call in tree_reads] == [{"format": "concise"}, None]


def test_uat_shaped_play_config_repairs_and_preserves_timing_contract(tmp_path):
    fixture_path = (
        Path(__file__).parent
        / "fixtures"
        / "studio_play_config_uat_shape_redacted.json"
    )
    response = json.loads(fixture_path.read_text(encoding="utf-8"))
    original_play_config = response["play_config"]
    client = MutableFakeStudioClient(response)

    report = auditor.run_audit_request(
        tenant="uat",
        course_id=COURSE_KEY,
        tenant_config_path=tenant_config(tmp_path),
        reader_factory=FakeReaderFactory(client),
        working_directory=tmp_path / "uat-config-shape",
        output=io.StringIO(),
    )

    expected_play_config = copy.deepcopy(original_play_config)
    expected_play_config["time_config"][0]["subtitle"] = "[REDACTED page 1 subtitle]"
    assert client.updated_configs == [expected_play_config]
    updated_play_config = client.updated_configs[0]
    assert updated_play_config["show_subtitles"] == original_play_config["show_subtitles"]
    for original_page, updated_page in zip(
        original_play_config["time_config"], expected_play_config["time_config"]
    ):
        assert (updated_page["page"], updated_page["start"], updated_page["end"]) == (
            original_page["page"], original_page["start"], original_page["end"]
        )

    update_index = client.calls.index(("update_play_config", GALLERY))
    publish_index = client.calls.index(("publish_vertical", VERTICAL))
    audit_indices = [
        i for i, call in enumerate(client.calls) if call[0] == "get_course_structure"
    ]
    assert len(audit_indices) == 2
    assert update_index < publish_index < audit_indices[-1]
    marker_check = next(
        item
        for item in report["target_sections"][0]["checks"]
        if item["check_id"] == "NLMCA-010"
    )
    assert marker_check["status"] == "pass"
    assert "[REDACTED page 1 subtitle]" not in json.dumps(report, ensure_ascii=False)
