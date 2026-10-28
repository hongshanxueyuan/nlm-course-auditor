import json

from scripts.nlm_course_auditor import audit_course


COURSE_KEY = "course-v1:FIRAx+1040663+20261018"
VERTICAL = "block-v1:FIRAx+1040663+20261018+type@vertical+block@lesson1"
GALLERY = "block-v1:FIRAx+1040663+20261018+type@imagesgallery+block@gallery1"


class FakeStudioReader:
    studio_base = "https://studio.example.test"

    def __init__(self, *, course_tree=None, gallery_overrides=None):
        self.course_tree = course_tree or course_tree_with_one_target()
        self.gallery_overrides = gallery_overrides or {}
        self.calls = []

    def get_course_structure(self, course_id):
        self.calls.append(("get_course_structure", course_id))
        return self.course_tree

    def get_files(self, gallery_locator):
        self.calls.append(("get_files", gallery_locator))
        return self.gallery_overrides.get(
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
        return self.gallery_overrides.get(
            "play_config", {"play_config": {"time_config": [{"page": 1}, {"page": 2}]}}
        )

    def get_audios(self, gallery_locator):
        self.calls.append(("get_audios", gallery_locator))
        return self.gallery_overrides.get("audios", {"audios": [{"id": "audio-secret"}]})


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
    assert report["pass_count"] == 5
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
    ]
    assert all(check["status"] == "pass" for check in section["checks"])
    assert section["checks"][2]["actual"] == {"valid_file_count": 2, "total_count": 2}
    assert section["checks"][3]["actual"] == {"time_config_count": 2, "valid_file_count": 2}
    assert section["checks"][4]["actual"] == {"audio_binding_count": 1}

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
    assert [check["status"] for check in checks[2:]] == ["issue"] * 6
    assert report["status"] == "issue"
    assert report["issue_count"] == 8
    assert report["unverified_count"] == 0


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
    assert [check["status"] for check in checks] == ["issue", "issue"]
    assert [check["actual"] for check in checks] == [{"count": 0}, {"count": 0}]
    assert report["status"] == "issue"
    assert report["issue_count"] == 2
    assert reader.calls == [("get_course_structure", COURSE_KEY)]


def test_course_with_no_target_verticals_is_not_reported_as_pass(tmp_path):
    reader = FakeStudioReader(course_tree={"id": COURSE_KEY, "category": "course", "child_info": {"children": []}})

    report = audit_course(
        tenant="uat",
        course_id=COURSE_KEY,
        studio_reader=reader,
        working_directory=tmp_path,
        audited_at="2026-10-28T01:02:03Z",
    )

    assert report["status"] == "unverified"
    assert report["target_sections"] == []
    assert report["course_findings"][0]["actual"] == {"target_section_count": 0}
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
