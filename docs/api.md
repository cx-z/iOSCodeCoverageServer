# iOS Coverage Platform HTTP API

实现定位从 [知识库入口](../knowledge/README.md) 开始；本文件继续作为 HTTP 路径、字段、状态码和错误码的事实源。

协议版本：`schema_version = 1`。服务根地址为 `http://<Mac-LAN-IP>:8765`；当前局域网 IP 以启动脚本输出为准。`GET /health` 和不含数据的 HTML 外壳公开；JSON 接口按以下两类凭证鉴权：

Cycle 的 `base_sha` 保留为首次提测审计字段；每个 Build 使用必填的 `git_sha`、`target_sha_at_build` 和 `merge_base_sha` 固化自己的比较快照，并用 `coverage_binary` 固化唯一分析主体。iPhone 只调用 Run 与网络探针接口；Build 大文件由 Mac 打包脚本注册。

```text
Authorization: Bearer <token>
```

| Token | 允许接口 |
| --- | --- |
| `<runtime_root>/state/operator_token` 操作员 Token | 所有现有管理、Build、Run、analysis 和报告接口 |
| `<runtime_root>/state/run_upload_token` iOS Token | `POST /api/v1/probes`、`POST /api/v1/runs` |

有效 iOS Token访问其他接口返回 HTTP 403 `insufficient_scope`；无效 Token返回 HTTP 401 `invalid_token`。GitLab PAT 只由服务端读取固定 MR GET，不是 Coverage HTTP 凭证。

JSON 响应均为 UTF-8、`Cache-Control: no-store`。错误响应至少包含稳定的 `error` 代码和可读 `message`。客户端不应依赖 `message` 文案。

## 1. Cycle

### 从 MR URL 创建或复用（推荐）

```bash
curl --fail-with-body \
  -X POST 'http://<Mac-LAN-IP>:8765/api/v1/cycles' \
  -H 'Authorization: Bearer <TOKEN>' \
  -H 'Content-Type: application/json' \
  --data '{
    "schema_version": 1,
    "mr_url": "https://git.example.com/mobile/example-app/merge_requests/42/diffs"
  }'
```

允许注册表内项目的 MR 详情、`/diffs`、`/commits` URL；query 和 fragment 会被忽略。仅接受 HTTPS、无 userinfo/端口且 host/project path 能唯一匹配 ProjectRegistry 的 URL。服务端从 `<runtime_root>/state/gitlab_api_token` 发起该项目固定只读 GET，调用方不能指定 API 主机、路径、方法或请求头。

新建返回 HTTP 201：

```json
{
  "status": "created",
  "cycle": {
    "id": "e9746e18-ed7a-44e4-a20b-1a7df90d382b",
    "schema_version": 1,
    "project": "ExampleApp",
    "project_display_name": "Example App",
    "mr_iid": "42",
    "mr_url": "https://git.example.com/mobile/example-app/merge_requests/42",
    "target_branch": "master",
    "base_sha": "1111111111111111111111111111111111111111",
    "head_sha": "2222222222222222222222222222222222222222",
    "status": "active",
    "created_at": "2026-08-31T16:00:00+00:00",
    "updated_at": "2026-08-31T16:00:00+00:00",
    "build_count": 0,
    "run_count": 0
  }
}
```

同一 `project + mr_iid` 已存在时返回 HTTP 200，`status` 为 `reused`，并返回原 `cycle.id` 及其不可变审计字段。新建 Cycle 的 `project`、IID、canonical URL、目标分支、base/head SHA 均来自校验后的 GitLab MR。

常见稳定错误：

```json
{"error":"project_not_registered","message":"GitLab project is not registered"}
{"error":"gitlab_token_unavailable","message":"GitLab credential is unavailable"}
{"error":"gitlab_auth_failed","message":"GitLab credential was rejected"}
{"error":"mr_not_found","message":"merge request was not found"}
{"error":"mr_not_open","message":"merge request is not open for testing"}
{"error":"mr_diff_refs_not_ready","message":"merge request diff refs are not ready"}
```

GitLab 失败不会生成部分 Cycle，也不影响服务启动、Run 上传或既有报告。服务端不执行 fetch、checkout 或任何 GitLab 写操作。

### 完整 Cycle JSON

脚本调用方也可以提交 `schema_version`、已注册的 `project`、可选 `mr_iid/mr_url`、`target_branch`、`base_sha`、`head_sha`。该入口每次创建新 Cycle；未知项目返回 `project_not_registered`。新 UI 和日常流程只使用 MR URL 入口。Git SHA 必须为完整 40 位 SHA-1 或 64 位 SHA-256。

服务端生成的 `cycle.id` 是稳定、不可变的 `cycle_id`。`base_sha` 同时受到 API 白名单和 SQLite trigger 保护，但它的语义是首次提测基准（`initial_base_sha`），只用于审计，不是所有后续 Build 永久共用的报告比较基准。

### 查询和更新 head

```bash
curl --fail-with-body \
  -H 'Authorization: Bearer <TOKEN>' \
  'http://<Mac-LAN-IP>:8765/api/v1/cycles'

curl --fail-with-body \
  -H 'Authorization: Bearer <TOKEN>' \
  'http://<Mac-LAN-IP>:8765/api/v1/cycles/<cycle_id>'

curl --fail-with-body \
  -X PATCH 'http://<Mac-LAN-IP>:8765/api/v1/cycles/<cycle_id>' \
  -H 'Authorization: Bearer <TOKEN>' \
  -H 'Content-Type: application/json' \
  --data '{"schema_version":1,"head_sha":"3333333333333333333333333333333333333333"}'
```

列表按 `updated_at`、`id` 从新到旧稳定排序，每项包含 `project`、展示用 `project_display_name`、MR 身份、`target_branch`、状态、时间及实时聚合的 `build_count`、`run_count`。只读投影还返回注册表确定的 `gitlab_host` 与 `gitlab_project_path`，供浏览器在创建 MR 外链前执行精确 host/project 校验；这些展示字段和计数均不做冗余持久化。

PATCH 只接受 `schema_version` 和 `head_sha`；携带 `base_sha`、`id` 或其他字段会返回 HTTP 400。

## 2. Build 注册

`POST /api/v1/builds` 接受以下 multipart 字段：

- 文本字段 `manifest`：下面的 JSON 对象；
- 必需文件字段 `main_executable`：精确主程序，上传文件名必须与 manifest 的安全 basename 一致；
- 必需文件字段 `dsym`：该 Build 精确 dSYM 的 ZIP，上传文件名必须与 manifest 一致并以 `.zip` 结尾；
- 条件可选文件字段 `debug_dylib`：仅在真实产物存在且 manifest 同时声明时上传，文件名必须与 descriptor 一致。

未知或重复字段会被拒绝。manifest 的 `artifacts`、`macho_uuids` 与 multipart 必须描述完全相同的产物集合。不存在的 dylib 不得传空值、占位数据、主程序复制品或伪造描述。

Debug Archive 的 manifest 示例（无 debug dylib）：

```json
{
  "schema_version": 1,
  "build_id": "6ee8cc4c-02e3-4eaf-91ab-f0f8128011e1",
  "cycle_id": "e9746e18-ed7a-44e4-a20b-1a7df90d382b",
  "git_sha": "2222222222222222222222222222222222222222",
  "target_sha_at_build": "3333333333333333333333333333333333333333",
  "merge_base_sha": "1111111111111111111111111111111111111111",
  "coverage_binary": "main_executable",
  "bundle_id": "com.example.product",
  "app_version": "1.0.0",
  "bundle_version": "100",
  "xcode_version": "26.3",
  "swift_version": "6.2",
  "macho_uuids": {
    "main_executable": ["269a5d10-032b-47b3-948a-738f3c1f0112"],
    "dsym": ["269a5d10-032b-47b3-948a-738f3c1f0112"]
  },
  "artifacts": {
    "main_executable": {
      "filename": "ExampleApp",
      "size": 123456,
      "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
    },
    "dsym": {
      "filename": "ExampleApp.app.dSYM.zip",
      "archive_format": "zip",
      "size": 345678,
      "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
    }
  }
}
```

`coverage_binary` 合法值只有：

- `main_executable`：后续 `llvm-cov` 只使用主程序作为分析主体。`debug_dylib` 可以不存在；如果确实存在，可以同时加入 `artifacts`、`macho_uuids` 和 multipart 保存，但不会改变分析主体。
- `debug_dylib`：后续 `llvm-cov` 只使用该逻辑角色对应的真实 dylib 作为分析主体。此时 manifest 和 multipart 都必须包含真实 dylib；主程序仍然必需。

普通 Debug Build 使用第二种形态：在上例中把 `coverage_binary` 改为 `debug_dylib`，并在 `artifacts` 与 `macho_uuids` 增加同名 `debug_dylib` 项，同时上传该文件。dSYM UUID 集合必须覆盖被选择分析主体的全部 Mach-O UUID。

三个 Git 字段的固定语义：

- `git_sha`：实际构建个人分支安装包的 head；
- `target_sha_at_build`：Archive 开始前，将 Cycle 的 `target_branch` 解析得到的目标分支 tip；
- `merge_base_sha`：同一时刻执行 `git merge-base <target_sha_at_build> <git_sha>` 的结果。

三个值随 Build manifest 一起不可变。目标分支在打包后推进，不得更新旧 Build manifest，也不得改变旧 Build 报告；后续只显示目标分支已推进的状态。个人分支同步目标分支后必须注册新 Build。

UUID 字段是数组，以支持多架构 Mach-O。每个产物的 `size` 和 `sha256` 必须对应实际上传字节。服务端使用 Xcode 26.3 `dwarfdump --uuid` 检查主程序和可选 dylib；dSYM ZIP 在受限临时目录中逐项安全解压，拒绝绝对路径、`..`、反斜杠、符号链接、特殊文件、重复目标、加密项和超限展开内容，再从实际 DWARF 文件提取 UUID。声明 UUID 必须与实际文件完全一致，且 `coverage_binary` 的 UUID 必须全部存在于 dSYM UUID 集合中。临时目录无论成功或失败都会清理。

`git_sha`、`target_sha_at_build` 和 `merge_base_sha` 都必须是完整 Git SHA。服务端经 `Build.cycle_id -> Cycle.project -> ProjectRegistry` 选择可信仓库，确认三个 commit object 存在并核对声明的 merge-base；manifest/HTTP 不接受仓库路径或 Git 命令。`bundle_id` 必须精确属于该项目的 `allowed_bundle_ids`。

稳定失败规则：

- 必填 Build 字段缺失：HTTP 400 `missing_build_field`；
- 任一 Git commit object 不存在：HTTP 422 `git_object_not_found`；
- 两个提交无法得到 merge-base：HTTP 422 `merge_base_unavailable`；
- 声明值与实际 merge-base 不一致：HTTP 422 `merge_base_mismatch`；
- multipart 与 manifest 产物集合不一致：HTTP 400 `artifact_set_mismatch`；
- Mach-O/dSYM UUID 无法读取或与声明不一致：HTTP 422 `artifact_uuid_unreadable` / `artifact_uuid_mismatch` / `dsym_uuid_unreadable`；
- 分析主体 UUID 不在 dSYM 中：HTTP 422 `coverage_binary_dsym_uuid_mismatch`；
- dSYM ZIP 无效或不安全：HTTP 422 `invalid_dsym_zip` / `unsafe_dsym_zip`；
- 同一 `build_id` 对应不同 manifest（包括 `coverage_binary`、产物集合或任一比较快照字段变化）：HTTP 409 `build_id_conflict`。

请求示例：

```bash
curl --fail-with-body \
  -X POST 'http://<Mac-LAN-IP>:8765/api/v1/builds' \
  -H 'Authorization: Bearer <TOKEN>' \
  --form 'manifest=<build_manifest.json' \
  --form 'main_executable=@/path/to/DemoApp;filename=DemoApp' \
  --form 'dsym=@/path/to/DemoApp.app.dSYM.zip;filename=DemoApp.app.dSYM.zip'
```

只有真实存在并在 manifest 声明时，才附加：

```bash
--form 'debug_dylib=@/path/to/DemoApp.debug.dylib;filename=DemoApp.debug.dylib'
```

首次成功为 HTTP 201；相同 `build_id` 和相同 manifest 重传为 HTTP 200：

```json
{
  "status": "registered",
  "build_id": "6ee8cc4c-02e3-4eaf-91ab-f0f8128011e1",
  "cycle_id": "e9746e18-ed7a-44e4-a20b-1a7df90d382b",
  "artifact_state": "ready",
  "pending_runs_promoted": 0,
  "idempotent_replay": false
}
```

相同 `build_id` 但 manifest 不同返回 HTTP 409 `build_id_conflict`。三个 Git 字段、`coverage_binary` 或实际产物集合任一不同都不能用重传修改。可用 `GET /api/v1/builds/<build_id>` 查询完整 manifest、固定比较快照、工具链、实际 UUID 和 SHA。无 dylib 的 Archive 查询示例：

```json
{
  "build": {
    "id": "6ee8cc4c-02e3-4eaf-91ab-f0f8128011e1",
    "cycle_id": "e9746e18-ed7a-44e4-a20b-1a7df90d382b",
    "schema_version": 1,
    "git_sha": "2222222222222222222222222222222222222222",
    "target_sha_at_build": "3333333333333333333333333333333333333333",
    "merge_base_sha": "1111111111111111111111111111111111111111",
    "coverage_binary": "main_executable",
    "bundle_id": "com.example.DemoApp",
    "app_version": "1.0.0",
    "bundle_version": "100",
    "xcode_version": "26.3",
    "swift_version": "6.2",
    "artifact_state": "ready",
    "manifest_sha256": "eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee",
    "main_executable_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "main_executable_size": 123456,
    "dsym_sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc",
    "dsym_size": 345678,
    "main_executable_uuids": ["269a5d10-032b-47b3-948a-738f3c1f0112"],
    "dsym_uuids": ["269a5d10-032b-47b3-948a-738f3c1f0112"],
    "artifacts": {
      "main_executable": {
        "filename": "DemoApp",
        "size": 123456,
        "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
      },
      "dsym": {
        "filename": "DemoApp.app.dSYM.zip",
        "archive_format": "zip",
        "size": 345678,
        "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
      }
    },
    "macho_uuids": {
      "main_executable": ["269a5d10-032b-47b3-948a-738f3c1f0112"],
      "dsym": ["269a5d10-032b-47b3-948a-738f3c1f0112"]
    },
    "manifest": {
      "schema_version": 1,
      "build_id": "6ee8cc4c-02e3-4eaf-91ab-f0f8128011e1",
      "cycle_id": "e9746e18-ed7a-44e4-a20b-1a7df90d382b",
      "git_sha": "2222222222222222222222222222222222222222",
      "target_sha_at_build": "3333333333333333333333333333333333333333",
      "merge_base_sha": "1111111111111111111111111111111111111111",
      "coverage_binary": "main_executable",
      "bundle_id": "com.example.DemoApp",
      "app_version": "1.0.0",
      "bundle_version": "100",
      "xcode_version": "26.3",
      "swift_version": "6.2",
      "macho_uuids": {
        "main_executable": ["269a5d10-032b-47b3-948a-738f3c1f0112"],
        "dsym": ["269a5d10-032b-47b3-948a-738f3c1f0112"]
      },
      "artifacts": {
        "main_executable": {
          "filename": "DemoApp",
          "size": 123456,
          "sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        },
        "dsym": {
          "filename": "DemoApp.app.dSYM.zip",
          "archive_format": "zip",
          "size": 345678,
          "sha256": "cccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccccc"
        }
      }
    },
    "pending_runs_promoted": 0,
    "created_at": "2026-09-01T03:00:00+00:00"
  }
}
```

示例展示了版本、工具链、实际产物集合、manifest SHA、时间和 pending Run 提升数量。未上传 dylib 时，`artifacts`、`macho_uuids` 及顶层字段均不伪造 dylib 信息。

## 3. Run 上传

Run 路径、multipart 字段、metadata schema、状态码、成功响应和幂等规则独立于后台分析。iPhone 只上传 Run metadata、独立 `.profraw` 和 SHA-256，不上传 Build 大文件，也不调用分析接口。

`POST /api/v1/runs` 必须恰好包含：文本 `metadata`、文本 `sha256`、文件 `profile`。profile 文件名必须是 `<run_id>.profraw`。

metadata 的稳定 schema：

```json
{
  "schema_version": 1,
  "run_id": "384212e0-7449-4ab7-813c-13dda5fd89ee",
  "cycle_id": "e9746e18-ed7a-44e4-a20b-1a7df90d382b",
  "build_id": "6ee8cc4c-02e3-4eaf-91ab-f0f8128011e1",
  "device_id": "4634f017-bfb4-4331-aa7c-d3ed453a3c36",
  "process_id": 4321,
  "launch_id": "bc26e845-d773-40ec-b328-a14263e47c30",
  "captured_at": "2026-08-31T16:20:00+08:00",
  "app_state": "active",
  "trigger_reason": "user_upload"
}
```

除 `app_state` 和 `trigger_reason` 可省略外，其余字段必填。所有 ID 都是 UUID；`process_id` 是正整数；`captured_at` 必须包含时区。请求示例：

```bash
PROFILE_SHA256="$(shasum -a 256 '<run_id>.profraw' | awk '{print $1}')"

curl --fail-with-body \
  -X POST 'http://<Mac-LAN-IP>:8765/api/v1/runs' \
  -H 'Authorization: Bearer <TOKEN>' \
  --form 'metadata=<run_metadata.json' \
  --form "sha256=${PROFILE_SHA256}" \
  --form 'profile=@<run_id>.profraw;filename=<run_id>.profraw'
```

已注册 Build 的首次上传返回 HTTP 201：

```json
{
  "status": "accepted",
  "run_id": "384212e0-7449-4ab7-813c-13dda5fd89ee",
  "cycle_id": "e9746e18-ed7a-44e4-a20b-1a7df90d382b",
  "build_id": "6ee8cc4c-02e3-4eaf-91ab-f0f8128011e1",
  "sha256": "dddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddddd",
  "size": 456789,
  "received_at": "2026-08-31T08:20:04+00:00",
  "run_state": "uploaded",
  "build_registered": true,
  "analyzable": true,
  "idempotent_replay": false
}
```

Build 未注册时，完整文件仍持久化，返回 HTTP 202、`run_state: pending_build`、`build_registered: false`、`analyzable: false`。以后注册同一 Cycle 的 Build 时自动提升。只有实际提升数量大于 0 时才请求后台分析；仍为 pending 的 Run 不触发。

相同 `run_id`、相同规范化 metadata、相同 Cycle/Build 和相同 profile SHA 重传返回 HTTP 200，且 `idempotent_replay: true`。JSON 空白和对象 key 顺序不影响 fingerprint；字段值变化会导致 HTTP 409 `run_id_conflict`。同一 `build_id` 已注册在其他 Cycle 时返回 HTTP 400 `build_cycle_mismatch`。

首次成功保存且已经 `uploaded/analyzable` 的 Run 会在持久化提交后请求该 Cycle 的后台分析。HTTP 成功只表示 Run 文件和 SQLite 已安全提交，不等待 LLVM；后台失败也不会反向把 Run 改成失败。完全幂等重放不会重复触发。iOS 继续按原响应完成本地上传队列，不需要解析分析状态。

## 4. 客户端响应处理规则

iOS 上传客户端应按以下规则实现：

1. 仅把 HTTP 200/201/202 且 `status == "accepted"` 的响应视为服务端已持久接收。
2. 必须核对响应的 `run_id`、`build_id`、`cycle_id`、`sha256`、`size` 均与本地记录一致；任何一项不一致都不能标记本地记录已上传。
3. HTTP 202 表示服务器已安全保存，但 Build 待注册；本地上传队列可结束该 Run 的网络重试，同时 UI 应提示 Build 未注册。
4. 网络错误、超时和 5xx 可以用原 metadata 和原 profile 有限重试；不得重新生成 `run_id`。
5. HTTP 401/403 停止本轮上传并提示 Token；HTTP 409 是永久身份冲突；HTTP 413 是大小配置问题；HTTP 422 表示 size/SHA、Build Git 快照、UUID 或 dSYM ZIP 校验失败，按稳定 `error` 代码处理。
6. multipart 字段名和 JSON 字段必须严格使用本文件 schema；服务端会拒绝重复、多余和拼错字段。

## 5. 独立分析状态接口

以下接口只供 Mac 操作人员、调试工具或报告页面使用，均要求操作员 Token；iOS Run Token 返回 403：

```text
GET  /api/v1/cycles/<cycle_id>/analysis
POST /api/v1/cycles/<cycle_id>/analysis/retry
```

GET 对从未请求分析的已知 Cycle 返回 HTTP 200 和显式 `idle`；queued、running、failed 时仍保留上一次成功引用：

```json
{
  "cycle_id": "<cycle_id>",
  "status": "succeeded",
  "requested_generation": 1,
  "completed_generation": 1,
  "attempt_count": 1,
  "last_trigger": "manual_retry",
  "latest_run_id": null,
  "current_build_id": "<build_id>",
  "orchestration_input_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "cycle_input_set_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
  "latest_success": {
    "generation": 1,
    "current_build_id": "<build_id>",
    "orchestration_input_sha256": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
    "cycle_input_set_sha256": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb",
    "summary": {
      "changed_new_side": 2960,
      "measurable": 1577,
      "covered": 955,
      "uncovered": 622,
      "not_measurable": 1383,
      "unknown": 0,
      "deleted_excluded": 8,
      "inherited": 420,
      "covered_with_inherited_evidence": 420,
      "changed_line_coverage_percent": 60.5580215599239
    }
  },
  "error": null
}
```

retry 必须是无请求体 POST，成功入队返回 HTTP 202 和当前状态文档；它不接受 Build、Run、Git SHA、仓库、命令或输出路径。queued/running 时 retry 只增加 generation，由同一个 worker 合并执行，不启动并发分析。未知 Cycle 返回 HTTP 404 `cycle_not_found`；非空请求体返回 HTTP 400 `request_body_not_allowed`。

状态响应不包含 Token、Mach-O 路径、命令输出或内部绝对输出路径。完整 generation、失败保留和服务重启恢复规则见 [后台自动分析](automatic_analysis.md)。

服务还提供只读报告接口：

```text
GET /api/v1/cycles/<cycle_id>/report
GET /api/v1/cycles/<cycle_id>/report/files/<file_id>
```

二者同样要求 Bearer Token。`file_id` 是服务端签发的仓库相对路径 SHA-256，不接受路径、Git SHA、Build、Run、命令或输出目录。HTML 入口 `GET /cycles/<cycle_id>` 不携带报告数据，页面取得 Token 后才调用上述接口。完整字段和安全边界见 [本地覆盖率报告页](coverage_report_ui.md)。

## 6. 大小、原子性和当前边界

- `--max-file-mib`：网络探针，默认 5 MiB；
- `--max-run-mib`：单个 `.profraw`，默认 100 MiB；
- `--max-build-artifact-mib`：主程序、debug dylib、dSYM ZIP 各自的上限，默认 512 MiB；
- multipart 请求还允许固定 1 MiB 元数据/边界开销；Build 总请求上限为三个产物上限之和再加开销。
- dSYM 解压后的所有普通文件总大小也不能超过单个 Build 产物上限，防止 ZIP 异常膨胀。

上传先进入 `data/.staging`，逐块计算 SHA、校验 size、`fsync` 后原子移动。SQLite 写操作使用事务；数据库不保存二进制内容。

上传阶段校验声明格式、文件完整性、三个 Git commit object、声明的 merge-base、实际 Mach-O/dSYM UUID，以及分析主体与 dSYM 的 UUID 对应关系。上传阶段不检查 `__llvm_covmap`、`__*llvm_prf*` sections 或 `.profraw` 的 LLVM 级兼容性；coverage sections 由打包脚本校验，profile 兼容性由分析器校验。`llvm-cov` 始终只使用 `coverage_binary` 指定的主体。
