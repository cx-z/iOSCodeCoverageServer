# 本地覆盖率报告页

代码定位见 [报告工作流](../knowledge/workflows/coverage_report.md) 和 [Web 模块](../knowledge/modules/web.md)。

Mac 服务提供一个低依赖、只读的 Cycle 报告页。它只展示后台最新成功 generation 已引用的、`complete.json` 完整性校验通过的累计结果，不运行 LLVM，也不修改 SQLite 或分析结果。

完整的日常提测顺序见 [iOS 真机覆盖率日常使用](daily_workflow.md)。

`/cycles/` 提供 Cycle 列表与 MR 创建/查找入口，`/cycles/<cycle_id>` 提供详情。两页在同一 origin 下复用同一个 `sessionStorage` 操作员 Token。

## 打开方式

服务启动后，在 Mac 浏览器打开：

```text
http://127.0.0.1:8765/cycles/
```

局域网内也可以使用 `http://<Mac-LAN-IP>:8765/cycles/`。`/cycles` 会重定向到带尾斜杠的路径。列表和详情 HTML 都是不含 Cycle/报告数据的公开外壳；页面要求输入 `<runtime_root>/state/operator_token` 操作员 Token。Token 只以 `ios-coverage-operator-token` 键写入当前标签页的 `sessionStorage`，不进入 URL、Cookie、HTML 或日志，关闭标签页后即消失。iOS Run Token和 GitLab PAT不能用于页面。

列表按最近更新时间展示项目名称/project/MR、可信 MR 链接、目标分支、状态、Build/Run 数、更新时间、项目筛选、详情入口和 Cycle ID 复制按钮。顶部只提交 `schema_version + mr_url`；浏览器不直接访问 GitLab，创建或复用成功后进入详情。所有服务端文字通过 DOM `textContent` 展示，MR 链接还会再次校验为 HTTPS MR 页面。

## 页面能力

- 每 2 秒查询一次后台分析状态，展示 queued/running/succeeded/failed 与请求、开始或完成时间；
- 展示 covered、uncovered、not_measurable、unknown 和可测量覆盖率；
- 按精确仓库相对路径列出变更文件，并展示逐文件四类计数；
- 点击文件查看 Git 新侧变更行、行号、源码文本、状态和覆盖来源；
- 覆盖来源区分 `当前 Build`、`历史继承`、`当前 + 历史` 和无来源；未完全覆盖的行会明确标为“部分 region”，避免把一个正数外层 region 误解为整行已覆盖；
- “只看未覆盖代码”同时过滤文件和当前文件明细；
- queued/running/failed 时仍展示上一次 latest success，不把部分或失败结果冒充新报告。

## HTTP 边界

以下 JSON 接口都要求操作员 Bearer Token：

```text
GET /api/v1/cycles/<cycle_id>/analysis
GET /api/v1/cycles/<cycle_id>/report
GET /api/v1/cycles/<cycle_id>/report/files/<file_id>
```

`file_id` 是服务端对已验证仓库相对路径计算的 SHA-256，不是路径。请求方不能传本机文件路径、Git SHA、Build、Run、仓库、命令或输出目录。服务端从 SQLite latest success 的不可变 identity 推导固定 `analysis/cycle-latest` 与 `analysis/cycle-coverage` 目录，并校验两个结果目录的完整输出集合、size 和 SHA-256 后才返回数据。绝对路径、`..`、符号链接、未知来源类型、篡改或缺失完成标记都会被拒绝。

报告 API 不返回 Mach-O/dSYM/profraw 路径、内部命令、Token 或任意文件内容。源码文本仅来自既有累计报告中已经固定的 Git 新侧变更行。

Cycle 列表还调用 `GET /api/v1/cycles`；创建表单调用 `POST /api/v1/cycles`。二者同样只接受操作员 Token。

当前 Build 删除的文件和删除行不属于 Git 新侧分母，因此不会继续出现在文件列表。内部不可变分析证据可能保留历史提交的 patch/source 文本以证明跨 Build 映射；这些审计文件不进入报告 JSON 或页面，也不能通过 file API 读取。

## 当前边界

这是可信局域网上的本机开发者报告页，不是多用户产品：没有账号系统、MR 评论、任意文件浏览、可变 latest 指针或自动上传行为。Cycle 创建只由 Mac 服务端读取固定 GitLab MR 元数据，页面本身不直接调用 GitLab；Run 上传仍只由用户在 App 中点击触发。
