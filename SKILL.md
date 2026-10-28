---
name: nlm-course-auditor
description: Audit a saved Studio course for the required “赋能内容” vertical, its “本节要点” HTML component, and ImageGallery structure. Use when an operator asks to screen an NLM course without relying on its local build folder.
---

# NLM Course Auditor

Run a read-only structural screen against Studio's saved authoring state. This checks the saved Studio course tree and Gallery handler data; it does not evaluate teaching quality, facts, wording, or media playback.

## Run an audit

1. From the audit working directory, provide a `tenant-config.json` with a `tenants` map. Each profile has a `studio_base` and may have a `courses_base`; see [the example](references/tenant-config.example.json).
2. Configure authentication through environment variables:
   - Account/password: `FIRA_SAAS_OP_ACCOUNT` and `FIRA_SAAS_OP_PASSWORD`.
   - Existing session: `FIRA_SAAS_OP_COOKIE`, plus `FIRA_SAAS_OP_CSRF_TOKEN` unless the cookie contains `csrftoken`.
3. Run the script with the configured profile key and full course key:

   ```powershell
   python "<nlm-course-auditor>/scripts/nlm_course_auditor.py" `
     --tenant uat `
     --course-id 'course-v1:FIRAx+1040663+20261018'
   ```

   If the tenant configuration is elsewhere, pass `--tenant-config <path>`. Use `--working-directory <path>` to choose where audit artifacts are written. Credentials are accepted from environment variables only, keeping them out of command history and process arguments.
4. Review the JSON report printed by the script. Open each target's `studio_url` to inspect the matching vertical in Studio.

The run is complete when the script prints a report and overwrites both per-course artifact files. A non-`pass` status needs operator review; a fatal input or course-structure read error exits without presenting an audit as passed.

The input `tenant` is a key in the configured tenant map. The complete course key is a separate identity value; the tool does not infer a tenant from its organization segment. This version accepts the explicit tenant and course key form.

## Read-only boundary and audit coverage

Each run requests the direct `GET /course/{course_id}?format=concise` route once, saves that fresh response, and discovers targets from that same snapshot. For each Gallery, the reader calls only `get_files`, `get_play_config`, and `get_audios`. These POST handlers read saved Studio data. The request client exposes no content mutation operation.

The audit checks every vertical whose trimmed Studio name equals `赋能内容`. It checks for exactly one ImageGallery and exactly one HTML component whose trimmed name equals `本节要点`. For each discovered Gallery, it compares valid file records with `total_count`, `play_config.time_config` entries with valid file records, and audio bindings with one. The stable IDs and passing standards are listed in [CHECKLIST.md](CHECKLIST.md).

Read errors and malformed handler responses remain `unverified`; confirmed count or component mismatches are `issue`. A readable course with no matching vertical is reported as `unverified` and cannot produce a passing result. A course-structure read failure stops the run without a pass report.

## Artifacts and report

The script creates or overwrites `auditor/<course-key-with-colon-replaced>/course-structure.json` and `report.json` under the working directory. `report.json` contains the tenant and course identity, schema version, audit time, overall status and separate result counts, course-level findings, and each target vertical with its direct Studio link and checks. Evidence contains counts, component names/categories, safe locators, and short read diagnostics. It omits API payloads, credentials, HTML bodies, media filenames and media URLs.

The course snapshot is the saved structure returned by Studio and is retained separately from the redacted report. A new run fetches and overwrites the snapshot and report; a stopped run is restarted as a fresh audit.
