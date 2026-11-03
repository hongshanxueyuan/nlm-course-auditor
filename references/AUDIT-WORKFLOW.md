# 审核流程与读取边界

## 课程读取

审核目标是 Studio 中已保存的编辑状态。每次完整审核分别读取：

- `GET /course/{course_id}?format=concise`：课程完整结构、目标发现和课程结构快照。
- `GET /course/{course_id}`：sequential 发布元数据；按 sequential locator 与结构树匹配。该响应不用于课程结构完整性判断。

每个目标 Gallery 分别读取 `get_files`、`get_play_config` 和 `get_audios`。所有检查项及通过标准见[检查清单](CHECKLIST.md)。

## 目标与定位

扫描课程中名称去掉首尾空格后等于“赋能内容”的 vertical。每个目标必须先具备有效的 `type@vertical` locator；locator 无效时将相关检查标为 `unverified`、留空 Studio 链接，并跳过 Gallery 读取。

## 读取失败

- Gallery 单项读取失败或 handler 响应无效时，只将依赖该数据的检查标为 `unverified`，其他独立检查继续运行。
- 课程发布元数据读取失败时，只把 sequential 发布状态标为 `unverified`，结构审核继续。
- 认证失败最多刷新四次。刷新后仍失败时停止后续读取并记录课程级 `NLMCA-007` `unverified`。
- 课程结构读取失败或结构无效时停止审核，不能将未完成的审核报告为通过。
- 课程树读取成功但没有匹配的目标 vertical 时，记录课程级 `NLMCA-006` `issue`、目标数 0，并提示核对租户、课程或命名。

普通运行和 `--audit-only` 都从新的课程读取开始。`--audit-only` 在初审报告后结束，不读取修复配置、不写入或发布 Studio 内容。

## 检查范围之外

程序通过认证的 Studio API 和 CSRF 处理读取与写入，不使用浏览器。它不判断课程事实、教学质量或措辞，也不试听、生成或修改音频。
