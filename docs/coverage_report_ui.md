# 本地覆盖率报告页

代码定位见 [报告工作流](../knowledge/workflows/coverage_report.md) 和 [Web 模块](../knowledge/modules/web.md)。

Mac 服务提供一个低依赖、只读的 Cycle 报告页。它只展示后台最新成功 generation 已引用的、`complete.json` 完整性校验通过的累计结果，不运行 LLVM，也不修改 SQLite 或分析结果。

完整的日常提测顺序见 [iOS 真机覆盖率日常使用](daily_workflow.md)。

`/login/` 提供登录与注册入口，`/cycles/` 是登录后的 Cycle 列表与 MR 创建/查找工作台，`/cycles/<cycle_id>` 提供详情；`/admin/users/` 提供管理员账号管理。浏览器只使用 HttpOnly Session，不持有机器 Token。

## 打开方式

服务启动后，在 Mac 浏览器打开：

```text
http://127.0.0.1:8765/login/
```

局域网内也可以使用 `http://<Mac-LAN-IP>:8765/login/`。认证页默认只显示登录表单，底部入口可切换为注册；注册成功直接建立 Session 并进入 `/cycles/`。未登录访问 `/cycles/` 会临时跳转到 `/login/`，已登录访问 `/login/` 会临时跳转到工作台。认证页、列表、账号管理和详情 HTML 都不嵌入业务数据；其中 Cycle 列表 HTML 只对有效 Web Session 提供。登录后服务端设置固定七天的 `HttpOnly; SameSite=Strict; Path=/` Cookie。原始 Session不进入 JavaScript、URL、HTML、日志或 SQLite 明文字段。

列表按最近更新时间展示项目名称/project/MR、可信 MR 链接、目标分支、状态、Build/Run 数、更新时间、项目筛选、详情入口和 Cycle ID 复制按钮。顶部只提交 `schema_version + mr_url`；浏览器不直接访问 GitLab，创建或复用成功后在工作台显示并可复制 Cycle ID，也可主动进入详情。所有服务端文字通过 DOM `textContent` 展示，MR 链接还会再次校验为 HTTPS MR 页面。

## 页面能力

- 每 2 秒查询一次后台分析状态，展示 queued/running/succeeded/failed 与请求、开始或完成时间；
- 展示 covered、uncovered、not_measurable、unknown 和可测量覆盖率；
- 按精确仓库相对路径列出变更文件，并展示逐文件四类计数；
- 点击文件查看 Git 新侧变更行、行号、源码文本、状态和覆盖来源；
- 覆盖来源区分 `当前 Build`、`历史继承`、`当前 + 历史` 和无来源；未完全覆盖的行会明确标为“部分 region”，避免把一个正数外层 region 误解为整行已覆盖；
- “只看未覆盖代码”同时过滤文件和当前文件明细；
- queued/running/failed 时仍展示上一次 latest success，不把部分或失败结果冒充新报告。

若整包 LLVM 代码区域因源码路径无法可靠映射而分析失败，页面显示失败状态并继续读取符合当前 diff refs 的旧成功报告；不会把这类失败渲染成全部 `not_measurable`。无编译器区域的真实不可测文件仍沿用原状态。

## HTTP 边界

analysis 状态要求 admin Session或 operator；report/file 在 `anonymous_read=true` 时无需平台登录，否则接受任一有效身份：

```text
GET /api/v1/cycles/<cycle_id>/analysis
GET /api/v1/cycles/<cycle_id>/report
GET /api/v1/cycles/<cycle_id>/report/files/<file_id>
```

`file_id` 是服务端对已验证仓库相对路径计算的 SHA-256，不是路径。请求方不能传本机文件路径、Git SHA、Build、Run、仓库、命令或输出目录。服务端从 SQLite latest success 的不可变 identity 推导固定 `analysis/cycle-latest` 与 `analysis/cycle-coverage` 目录，并校验两个结果目录的完整输出集合、size 和 SHA-256 后才返回数据。绝对路径、`..`、符号链接、未知来源类型、篡改或缺失完成标记都会被拒绝。

报告 API 不返回 Mach-O/dSYM/profraw 路径、内部命令、Token 或任意文件内容。源码文本仅来自既有累计报告中已经固定的 Git 新侧变更行。

GitLab MR 页面上的只读浏览器扩展先调用：

```text
POST /api/v1/integrations/gitlab/merge-request-coverage/resolve
```

服务端用已有 GitLab PAT 获取当前 MR 的完整 diff refs，并要求 latest success 的 current Build 满足 `merge_base_sha/base_sha`、`target_sha_at_build/start_sha`、`git_sha/head_sha` 三组完全相等。只有响应 `can_annotate=true` 时，扩展才能继续按 `summary_api` 和既有 file API 映射 `repo_relative_path + new_line_number`。任一 SHA 不同即为 `stale_diff`，不能按 head-only、路径、源码文本或行号猜测。

resolver 的读取同样由 `anonymous_read` 控制；它只接受 MR 页面 URL，不创建 Cycle、不触发分析、不修改报告，也不向浏览器暴露 GitLab PAT 或本机路径。完整响应与状态语义见 [HTTP API](api.md#6-gitlab-mr-coverage-只读解析)。

Cycle 列表还调用 `GET /api/v1/cycles`；创建表单调用 `POST /api/v1/cycles`。developer/admin 使用同源 Session，服务端而非前端实施数据隔离；operator 仅保留兼容路径。

当前 Build 删除的文件和删除行不属于 Git 新侧分母，因此不会继续出现在文件列表。内部不可变分析证据可能保留历史提交的 patch/source 文本以证明跨 Build 映射；这些审计文件不进入报告 JSON 或页面，也不能通过 file API 读取。

## 当前边界

这是可信局域网上的多用户 Cycle Web能力，但不是公网身份系统：没有 OAuth/SSO/MFA、MR 评论、任意文件浏览、可变 latest 指针或自动上传行为。Cycle 页面和浏览器扩展都不持有机器 Token或 GitLab PAT；Run 上传仍只由用户在 App 中点击触发。
