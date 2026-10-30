# NLM 课程审核检查清单

这些检查只核对 Studio 已保存课程中的客观结构，不判断内容事实、教学质量、表达质量或媒体能否播放。

## 目标组件

| ID | 检查项 | 范围 | 规则 | 通过标准 |
| --- | --- | --- | --- | --- |
| NLMCA-001 | 目标小节恰有一个 ImageGallery | 名称去掉首尾空格后等于 `赋能内容` 的每个 vertical | 统计其直接子组件中 `category` 为 `imagesgallery` 的组件数 | 数量恰为 1 |
| NLMCA-002 | 目标小节恰有一个“本节要点” HTML 组件 | 名称去掉首尾空格后等于 `赋能内容` 的每个 vertical | 统计直接子组件中 `category` 为 `html` 且名称去掉首尾空格后等于 `本节要点` 的组件数 | 数量恰为 1 |
| NLMCA-008 | ImageGallery 位于第一个直接子组件 | 每个目标 vertical | 检查直接子组件列表首项的 `category` 是否为 `imagesgallery` | 第一个直接子组件是 ImageGallery |
| NLMCA-009 | “本节要点”位于最后一个直接子组件 | 每个目标 vertical | 检查直接子组件列表末项的 `category` 是否为 `html` 且名称去掉首尾空格后等于 `本节要点` | 最后一个直接子组件是“本节要点” HTML |

位置检查独立于数量检查：ImageGallery 或“本节要点”缺失、重复时，NLMCA-001/002 报告数量问题，NLMCA-008/009 仍按首末直接子组件判定。直接子组件列表为空时，两项位置检查均为 `issue`。更深层的后代组件不计入直接子组件。

所有目标检查都要求有效的 `type@vertical` locator。如果 locator 缺失，该目标的检查记为 `unverified`，Studio 链接留空，并跳过 Gallery 读取；每条目标小节结构问题都包含 vertical 直达链接。

## ImageGallery 数据

| ID | 检查项 | 范围 | 规则 | 通过标准 |
| --- | --- | --- | --- | --- |
| NLMCA-003 | Gallery 文件数与 `total_count` 一致 | 目标 vertical 中的每个 ImageGallery | 确认每条文件记录均有效且 Studio 资源 ID 唯一，再比较有效文件记录数与 `total_count` | 两个数量相等 |
| NLMCA-004 | 时间配置条目数与有效文件记录数一致 | 目标 vertical 中的每个 ImageGallery | 比较 `play_config.time_config` 的条目数与有效文件记录数 | 两个数量相等 |
| NLMCA-005 | Gallery 恰有一个音频绑定 | 目标 vertical 中的每个 ImageGallery | 统计 `get_audios.audios` 中的绑定数 | 数量恰为 1 |

## 课程范围

| ID | 检查项 | 范围 | 规则 | 结果 |
| --- | --- | --- | --- | --- |
| NLMCA-006 | 发现目标 vertical | 每轮审核的一门课程 | 在新读取的整课结构树中查找名称去掉首尾空格后严格等于 `赋能内容` 的 vertical | 未找到目标时，记录一条课程级 `issue`，目标数为 0，并提示核对租户、课程或命名；不生成虚构的小节检查 |
| NLMCA-007 | 完成整课读取 | 每轮审核的一门课程 | 课程树无效、无法读取，或最多四次认证刷新后仍未恢复时停止审核 | 记录一条课程级 `unverified`；停止的审核不能报告为 `pass` |

每项结果的状态为 `pass`、`issue` 或 `unverified`。有充分证据确认违反规则时记为 `issue`；证据不足或读取失败时记为 `unverified`。Gallery 局部读取失败只影响依赖该结果的检查，其他独立检查继续执行。课程总体状态优先级为 `issue`、`unverified`、`pass`；只有所有结果均通过时才是 `pass`。报告中的失败信息仅包含脱敏后的阶段、类别、可用的 HTTP 状态码和简短安全摘要。
