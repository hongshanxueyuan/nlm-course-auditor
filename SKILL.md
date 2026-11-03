---
name: nlm-course-auditor
description: 审核 Studio 课程中 sequential 小节的发布状态，以及“赋能内容”小节结构、ImageGallery 数据和字幕风险；默认单轮修复已确认的字幕行首 > 标记。
disable-model-invocation: true
---

# NLM 课程审核

本 skill 检查 Studio 已保存课程中 sequential 小节的正式发布状态，以及“赋能内容”vertical 的结构、Gallery 数据和字幕风险。默认运行会先完整审核，再单轮修复获准的字幕行首 `>` 标记、发布实际修改的 vertical 并完整复审；使用 `--audit-only` 可只报告初审结果。修复条件与跳过规则见 [SUBTITLE-REPAIR.md](references/SUBTITLE-REPAIR.md)。本 skill 不判断学员端可访问性、教学质量、事实正确性、措辞质量，也不验证已生成音频的实际读音或媒体播放情况。

领域词汇和稳定术语见 [GLOSSARY.md](references/GLOSSARY.md)。

## 运行审核

1. 主站租户 `fira`（`https://studio.firacademy.com`）内置可用；使用主站课程 URL 或 `--tenant fira` 时无需准备配置文件。其他租户在审核工作目录准备 `tenant-config.json`，其中包含 `tenants` 配置对象。每个租户配置项设置 `studio_base`，也可设置 `courses_base`。配置示例见 [tenant-config.example.json](references/tenant-config.example.json)。
2. 选择一种课程定位方式。

   已配置的租户 key 和完整 course key：

   ```powershell
   python "<nlm-course-auditor>/scripts/nlm_course_auditor.py" `
     --tenant uat `
     --course-id 'course-v1:FIRAx+1040663+20261018'
   ```

   或使用 Studio 课程 URL / 小节 URL：

   ```powershell
   python "<nlm-course-auditor>/scripts/nlm_course_auditor.py" `
     --studio-url 'https://studio.example.com/course/course-v1:ORG+COURSE+RUN'
   ```

   URL 输入也支持 `/container/block-v1:...+type@vertical+block@...` 小节链接。URL 的 host 和 port 必须恰好匹配一个租户配置项；显式配置优先，未匹配时再尝试内置主站租户。请求会使用匹配配置中的 `studio_base`。程序会在创建认证客户端前拒绝不支持或不完整的 URL，以及同时填写两种课程定位方式的输入。
3. 通过环境变量配置认证：
   - 账号密码：`FIRA_SAAS_OP_ACCOUNT` 和 `FIRA_SAAS_OP_PASSWORD`。
   - 现有会话：`FIRA_SAAS_OP_COOKIE` 和匹配的 `FIRA_SAAS_OP_CSRF_TOKEN`；Cookie 中也可包含 `csrftoken`。

   如果租户配置文件不在当前目录，使用 `--tenant-config <path>` 指定。使用 `--working-directory <path>` 选择审核产物的保存位置。需要只生成报告时，在命令中加入 `--audit-only`。凭据只通过环境变量传入，避免出现在命令历史或进程参数中。使用账号密码登录时，程序会先在已配置的 Studio host 建立会话，再读取课程树。
4. 查看程序打印的 JSON 报告及课程专属产物。所有审核流程都通过程序和 Studio API 读取已保存数据；按规则生成的 `studio_url` 只作为报告链接提供。本 skill 的任何场景都不调用浏览器。

当程序打印最终报告并刷新课程专属产物时，本轮运行完成。`pass` 以外的状态都需要运营复核。输入错误会在认证前退出；课程结构读取失败或认证重试后仍未恢复时，程序会打印 `unverified` 报告并停止审核。

显式输入中的 `tenant` 是 `tenants` 配置对象的 key，完整 course key 是独立的课程身份。程序不会从 course key 的组织标识推断租户。使用 URL 输入时，程序只根据 host 选择唯一匹配的租户配置项。

## 检查范围与自动修复范围

整课课程树会单独扫描所有 `sequential` 小节的正式发布状态，不限于包含“赋能内容”的小节。结构树和发布元数据从两次不同格式的 `GET /course/{course_id}` 分开读取：`format=concise` 响应用于完整结构树、目标发现和课程快照；默认格式响应只用于读取发布字段，并按 sequential locator 与结构树匹配。只有 `published=true` 且 `has_changes=false` 才能确认当前编辑已发布；明确未发布或仍有未发布改动时记为问题，必要字段缺失或无效时无法确认。报告为每个 sequential 小节保留一条结果和 Studio 直达链接。

每轮完整审核读取一次 `GET /course/{course_id}?format=concise` 作为课程结构，并读取一次默认格式的 `GET /course/{course_id}` 作为发布元数据来源。默认格式响应不参与结构完整性校验；若该请求读取失败，发布状态检查记为无法确认，其他检查继续。账号密码登录时，程序可能向已配置的 Studio host 发起会话初始化请求以获取 CSRF cookie，再读取课程数据。初审对每个 Gallery 调用 `get_files`、`get_play_config` 和 `get_audios`。默认运行若发现可确认的 NLMCA-010 问题，会再读取完整播放配置，通过 ImageGallery 更新 handler 保存字幕，并发布至少有一次成功更新的 vertical；只审核模式跳过这些写入。每次 Studio POST 写入都使用认证客户端及 CSRF。具体保护条件见 [SUBTITLE-REPAIR.md](references/SUBTITLE-REPAIR.md)。

审核会扫描所有 Studio 名称去掉首尾空格后等于“赋能内容”的 vertical。每个目标必须恰有一个 ImageGallery，且它是第一个直接子组件；也必须恰有一个名称去掉首尾空格后等于“本节要点”的 HTML 组件，且它是最后一个直接子组件。中间的直接子组件不受顺序限制，更深层的后代组件不参与这些检查。对每个发现的 Gallery，程序会核对有效文件记录数与 `total_count`、`play_config.time_config` 条目数与有效文件记录数、音频绑定数是否为 1，扫描保存字幕中开头为 `>` 的大于号；检查可读字幕中的加粗格式是否识别成功（由 CommonMark 解析器判定）；并检查正文段落是否先于 Markdown 标题。NLMCA-004 和 NLMCA-011 在命中时会附带运营排查提示；提示不改变检查结果或证据。稳定 ID 和通过标准见 [CHECKLIST.md](references/CHECKLIST.md)。

验证目标前，必须先确认其 locator 是有效的 `type@vertical`。如果课程树中缺少有效 vertical locator，该目标的检查记为 `unverified`，Studio 链接留空，并跳过 Gallery 读取。每条目标小节存在的结构问题都会包含该 vertical 的 Studio 直达链接。

读取失败或 handler 响应格式无效时，结果为 `unverified`；确认的数量或组件不匹配时，结果为 `issue`。本地 Gallery 读取失败只会让依赖该读取的检查成为 `unverified`，其他独立检查会继续。默认格式发布元数据读取失败时，只将每个 sequential 发布状态标为 `unverified`，结构审核继续；认证失败最多刷新四次，认证重试后仍失败会停止后续读取，并增加课程级 `NLMCA-007` `unverified`。课程结构读取失败时也会停止审核。若课程树读取成功但没有匹配的 vertical，则记录课程级 `NLMCA-006` `issue`，目标数为 0，并提示运营核对租户、课程或命名规则。目标数为 0 时不会虚构小节检查。

## 产物与报告

程序会在工作目录下创建或覆盖 `auditor/<course-key-with-colon-replaced>/course-structure.json` 和 `report.json`。报告包含租户和课程身份、`schema_version`、审核时间、总体状态与各状态数量、课程级 `course_findings`、每个目标 vertical 的检查结果，以及 `sequential_sections` 中全课程每个 sequential 小节的发布检查和 Studio 直达链接。适用时，检查结果可单独包含 `operator_hint`；它是供运营排查的经验提示，不属于审核证据，也不代表根因已确认。位置检查的证据只保留直接子组件总数、目标组件的位置，以及首末组件的类别和名称；不保存完整组件序列。其他证据只保留数量、页码、组件名称/类别、安全 locator 和经过脱敏的简短读取诊断，不包含 API payload、原始异常或服务端响应、凭据、Cookie、token、HTML 正文、字幕原文、媒体文件名或媒体 URL。字幕标记或加粗标记发现会链接到对应 Studio 小节；这些检查只报告已保存字幕的文本风险，不推断已有 MP3 的实际读音。

课程快照单独保存为 Studio 返回的课程结构，并与脱敏报告分开。每轮新审核都会重新读取并覆盖快照和报告。如果 Studio 无法返回有效的新结构，程序会移除旧快照，避免旧数据被误认为当前结果。中断后重新运行会从新一轮审核开始。

面向运营的最终说明默认按检查结果分类：每类使用标题，只列出该类下存在问题或无法确认的小节元素，并附安全的 Studio 链接。按 `check_id` 使用以下标题：NLMCA-010“开头出现 >”；NLMCA-013“章节未发布”；NLMCA-012“加粗识别失败”；NLMCA-011“正文与 Markdown 标题顺序”；NLMCA-003/004/005“ImageGallery 相关问题”；NLMCA-001/002/008/009“小节组件结构”；NLMCA-006/007“课程级问题”。其他检查归入“其他问题”。标题表示检查类别；每条说明标记“已确认问题”或“暂时无法确认”，避免把无法确认写成已确认问题。同一类别下合并列出关联 sequential 小节或 vertical；多个目标都叫“赋能内容”时，结合课程结构快照中的上级单元名称区分。用户要求按小节查看时，改用 sequential 小节标题归组，并仍只列出存在问题或无法确认的元素。

报告使用自然、通俗的中文，不直接说“几个 issue”或照搬 `issue` / `unverified` 状态词、检查 ID 和状态数量。组件称呼使用 `imagegallery`。NLMCA-010 或 NLMCA-012 命中时列出页码和数量。NLMCA-010 命中时写“第 X 页出现 Y 个开头为 `>` 的大于号”；NLMCA-012 命中时写“第 X 页识别加粗失败 Y 处”。NLMCA-011 命中时先说明页码及“正文段落先于 Markdown 标题”的观察结果，再引用其 `operator_hint`，并明确这是可能原因和运营排查线索，不是审核确认的根因。其他结果包含 `operator_hint` 时，也要在说明客观结果后引用提示原文，并清楚表明它不是审核确认的根因。暂时无法核实时，说明无法确认的内容和对应提示。全部通过时，最终运营说明不输出成功提示或“未发现问题”之类的正文；报告和课程快照仍按上述路径保存。结构化 JSON 的状态字段保持不变。

例如：“1.3 小节里，imagegallery 目前排在第 3 位，应移到首位；‘本节要点’目前排在第 2 位，应移到末尾。请在 Studio 调整后复核。”若 NLMCA-004 适用，可在事实说明后补充：“大概率是 imagegallery 有几页图片未成功上传。”
