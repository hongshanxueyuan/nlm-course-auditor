# NLM Course Auditor checklist

These checks describe objective structure in the saved Studio authoring state. They do not judge content facts, teaching quality, expression quality, or whether media plays correctly.

## Target components

| ID | Check | Scope | Rule | Pass standard |
| --- | --- | --- | --- | --- |
| NLMCA-001 | Target vertical has one ImageGallery | Each vertical named `赋能内容` after trimming | Count direct child components with category `imagesgallery` | Exactly one |
| NLMCA-002 | Target vertical has one “本节要点” HTML component | Each vertical named `赋能内容` after trimming | Count direct child components with category `html` and trimmed name `本节要点` | Exactly one |

All target checks require a valid `type@vertical` locator. If it is missing, keep all five target checks `unverified`, leave the Studio link empty, and skip Gallery reads; each target-section `issue` includes a direct vertical link.

## ImageGallery data

| ID | Check | Scope | Rule | Pass standard |
| --- | --- | --- | --- | --- |
| NLMCA-003 | Gallery file count matches `total_count` | Each discovered ImageGallery in a target vertical | Validate every file record has a unique Studio asset identifier, then compare the valid record count to `total_count` | Counts are equal |
| NLMCA-004 | Timing-entry count matches valid files | Each discovered ImageGallery in a target vertical | Compare the number of entries in `play_config.time_config` to the valid file-record count | Counts are equal |
| NLMCA-005 | Gallery has one audio binding | Each discovered ImageGallery in a target vertical | Count entries in `get_audios.audios` | Exactly one |

## Course scope

| ID | Finding | Scope | Rule | Outcome |
| --- | --- | --- | --- | --- |
| NLMCA-006 | Target vertical discovery | One course per run | Search the fresh full-course tree for a vertical whose trimmed name is exactly `赋能内容` | Zero matches produce one course-level `issue` with target count zero and a tenant/course/name hint; no fabricated section checks |
| NLMCA-007 | Full course audit read | One course per run | Stop after an invalid/unreadable course tree or unresolved authentication after at most four refreshes | Record one course-level `unverified` finding; never present a stopped run as `pass` |

Each result is `pass`, `issue`, or `unverified`. A confirmed rule violation is an issue; missing or malformed evidence is unverified. Local Gallery read failures affect their dependent checks while independent checks continue. The overall course status prioritizes `issue`, then `unverified`, and is `pass` only if all results pass. Reports include only sanitized failure stage/category, available HTTP status, and a short safe summary.
