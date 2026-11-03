# 产物与报告

## 文件

在工作目录下创建或覆盖：

- `auditor/<course-key-with-colon-replaced>/course-structure.json`
- `auditor/<course-key-with-colon-replaced>/report.json`

结构快照保存 Studio 返回的课程结构，与脱敏报告分开。每轮审核重新读取并覆盖；若 Studio 无法返回有效的新结构，会删除旧快照，避免旧数据被误认为当前结果。

## 报告内容与安全边界

报告包含租户和课程身份、`schema_version`、审核时间、总体状态及状态数量、课程级 `course_findings`、每个目标 vertical 的检查结果，以及所有 sequential 小节的发布检查和 Studio 直达链接。普通运行发生写入时，`repair_summary` 还记录字幕修复、vertical 发布和 sequential 自动发布结果。检查规则与 ID 见[检查清单](CHECKLIST.md)。

报告只保留数量、页码、组件名称/类别、安全 locator 和脱敏后的简短读取诊断；不保存 API payload、原始异常或服务端响应、凭据、Cookie、token、HTML 正文、字幕原文、媒体文件名或媒体 URL。位置证据只包含组件总数、目标组件位置和首末组件类别及名称。

## 面向运营的说明

面向运营的报告使用通俗中文，按问题类别归组；只列出问题或无法确认的小节元素并附 Studio 链接。不要直接写“几个 issue”或照搬 `issue` / `unverified` 状态词、检查 ID 和状态数量。全部通过时，不输出“未发现问题”等成功正文，JSON 报告和课程快照仍照常保存。

NLMCA-010 命中时写“第 X 页出现 Y 个开头为 `>` 的大于号”；NLMCA-012 命中时写“第 X 页识别加粗失败 Y 处”。NLMCA-011 先列出页码及“正文段落先于 Markdown 标题”的观察结果，再引用 `operator_hint`，并说明它只是排查线索，不是审核确认的根因。其他带 `operator_hint` 的检查也先说明客观结果，再将提示表述为可能原因。无法确认时，说明具体无法确认的内容。

多个目标都叫“赋能内容”时，结合课程结构快照中的上级单元名称区分。用户要求按 sequential 小节查看时，改用小节标题归组，仍只列出问题或无法确认的项目。

运行完成的标准是最终 JSON 报告和课程专属产物已刷新。普通运行有修复时，以完整复审的最终状态为准；没有修复时，以初审状态为准。除 `pass` 外的状态都需运营复核。
