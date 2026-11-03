# 运行方式

## 课程定位

支持以下两种输入，且一次只使用一种：

- 租户 key 与完整 course key，例如 `--tenant uat --course-id 'course-v1:ORG+COURSE+RUN'`。
- Studio 课程 URL 或小节 URL，例如 `--studio-url 'https://studio.example.com/course/course-v1:ORG+COURSE+RUN'`。

URL 也可使用 `/container/block-v1:...+type@vertical+block@...` 小节路径。URL 的 host 和 port 必须唯一匹配一个租户配置。显式租户配置优先；未匹配时再尝试内置主站租户。程序不会根据 course key 中的组织标识推断租户。

主站租户 `fira` 内置可用。使用主站 URL 或 `--tenant fira` 无需额外租户配置。其他租户需在工作目录准备 `tenant-config.json`，内容包含 `tenants` 对象；每个租户设置 `studio_base`，可选设置 `courses_base`。格式见 [tenant-config.example.json](tenant-config.example.json)。

## 认证

认证值通过环境变量传入：

- 账号密码：`FIRA_SAAS_OP_ACCOUNT`、`FIRA_SAAS_OP_PASSWORD`。
- 现有会话：`FIRA_SAAS_OP_COOKIE`、匹配的 `FIRA_SAAS_OP_CSRF_TOKEN`；Cookie 中也可包含 `csrftoken`。

使用账号密码时，程序先在配置的 Studio host 建立会话并获取 CSRF cookie，再读取课程。不要把认证值放在命令参数中。

## 命令

课程 key：

```powershell
python "<nlm-course-auditor>/scripts/nlm_course_auditor.py" `
  --tenant uat `
  --course-id 'course-v1:ORG+COURSE+RUN'
```

Studio URL：

```powershell
python "<nlm-course-auditor>/scripts/nlm_course_auditor.py" `
  --studio-url 'https://studio.example.com/course/course-v1:ORG+COURSE+RUN'
```

租户配置不在当前目录时传入 `--tenant-config <path>`；用 `--working-directory <path>` 指定审核产物目录。显式只读审核时添加 `--audit-only`。
