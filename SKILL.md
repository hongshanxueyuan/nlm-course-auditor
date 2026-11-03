---
name: nlm-course-auditor
description: 审核 Studio 课程中 sequential 小节的发布状态，以及“赋能内容”小节结构、ImageGallery 数据和字幕风险；默认单轮修复获准的行首 > 标记与可安全修复的加粗格式。
disable-model-invocation: true
---

# NLM 课程审核

本 skill 检查 Studio 已保存课程中的 sequential 发布状态、“赋能内容”小节结构、ImageGallery 数据和字幕风险。默认运行会先完整审核；发现获准问题时最多修复一轮、发布实际修改的 vertical，并完整复审。`--audit-only` 只生成初审报告，不写入 Studio。检查范围不包括课程事实、教学质量、文字表达或已生成音频的实际读音。

## 工作流

1. **准备输入和认证。** 选择租户与课程 key 或 Studio URL，并配置认证。输入形式、配置文件和命令见 [运行方式](references/USAGE.md)。
2. **执行整课审核。** 普通运行会审核整门课程，并由 auditor 在同一次运行中判断是否有获准修复项。只需要报告时使用 `--audit-only`。读取范围和失败处理见 [审核流程](references/AUDIT-WORKFLOW.md)；检查 ID 和判定规则见 [检查清单](references/CHECKLIST.md)。
3. **按结果处理修复细节。** 普通运行只会处理初审确认的 NLMCA-010 或 NLMCA-012 候选，最多一轮，然后复审整门课程。使用 `--audit-only` 或没有获准候选时，不会写入 Studio，也不需要读取自动修复说明。仅当最终报告的 `repair_summary.targets` 非空，或用户询问修复、跳过、发布细节时，才读取[字幕修复规则](references/SUBTITLE-REPAIR.md)。
4. **查看最终报告。** 有修复时，以修复和发布后的整课复审为准；无修复时，以初审为准。产物位置、脱敏边界和运营说明格式见[报告规则](references/REPORTING.md)。

## 术语

遇到领域术语时查阅[领域词汇](references/GLOSSARY.md)。
