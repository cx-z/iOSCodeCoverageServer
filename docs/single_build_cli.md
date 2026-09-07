# 单 Build 本机 LLVM 分析 CLI

实现位于 `ioscoverage/analysis/single_build/`，根命令只是兼容入口；代码导航见 [Analysis 模块](../knowledge/modules/analysis.md)。

`analyze_single_build.py` 是受限的本机分析入口。它只分析 SQLite 已登记的一个 Build 和调用方明确列出的 Run，并产生完整 `llvm-cov export` JSON。它不是 HTTP API，不修改 SQLite，也不执行 MR diff、跨 Build 累计、报告 UI 或 GitLab 集成。

## 调用方式

```bash
cd /path/to/ios-coverage-server
python3 analyze_single_build.py \
  --runtime-root /absolute/runtime \
  --cycle-id <cycle_id> \
  --build-id <build_id> \
  --run-id <run_id_1> \
  --run-id <run_id_2>
```

`--run-id` 至少出现一次，不能重复。Run 参数顺序不影响输入集合身份。

CLI 不提供二进制、dSYM、profraw、Git 仓库、LLVM 工具或输出目录参数。路径由 runtime root 和 Cycle.project 解析：

```text
SQLite:       <runtime_root>/instance/coverage.sqlite3
服务端数据:   <runtime_root>/data
项目注册仓库: <runtime_root>/config/projects.json 中 Cycle.project 对应的 repository_path
Xcode:        /Applications/Xcode263.app/Contents/Developer
分析输出:     <runtime_root>/analysis/single-build
```

退出码：

- `0`：新分析成功或完整结果幂等复用；
- `2`：命令行参数格式错误，由 `argparse` 返回；
- `3`：Cycle/Build/Run、文件、UUID 或 Git 前置校验失败；
- `4`：Xcode/LLVM 命令、stderr 或 export 格式失败；
- `5`：证据存储、既有结果完整性或内部错误。

成功和失败都输出 JSON。成功结果包含 `input_set_sha256`、输出目录、`idempotent_replay` 和完整 summary；失败结果包含稳定 `error` 与安全说明。

## 分析前校验

SQLite 以 URI `mode=ro` 和 `query_only` 打开。LLVM 启动前必须全部满足：

- Cycle、Build 存在且归属一致，Build 为 `artifact_state=ready`；
- `coverage_binary` 只能是 `main_executable` 或 `debug_dylib`，且只使用 manifest 指定主体；
- manifest 文件、SQLite manifest、manifest SHA、产物集合完全一致；
- 全部实际 Build 产物 size/SHA 与 manifest、SQLite 一致；
- 复用服务端安全 ZIP 解压和 Xcode 26.3 `dwarfdump`，重新验证 Mach-O/dSYM UUID 及分析主体与 dSYM 的 UUID 包含关系；
- 每个 Run 属于指定 Cycle/Build，且 `state=uploaded`、`analyzable=true`、`error_message` 为空；
- 每个 Run 的 metadata、profraw size/SHA 和 `upload.sha256` 与 SQLite 一致；
- 注册项目仓库确实是配置指向的 Git 工作树根；
- Build 的 `git_sha`、`target_sha_at_build`、`merge_base_sha` 都是存在的 commit object，且声明的 merge-base 正确。

CLI 不读取当前 HEAD、分支名或工作树文件来形成覆盖结论。HEAD 可以指向其他提交，工作树也可以有未提交修改；这些状态不进入 `input_set_sha256`。CLI 不会 checkout、switch、reset、clean、stash 或创建/删除 Git worktree。

## LLVM 顺序和错误规则

所有命令使用参数数组调用 `subprocess.run`，不经过 shell，并设置：

```text
DEVELOPER_DIR=/Applications/Xcode263.app/Contents/Developer
```

固定顺序：

1. Xcode 26.3 与 `dwarfdump` 校验；
2. 记录 Xcode、`llvm-profdata`、`llvm-cov` 版本；
3. 对每个 profraw 分别执行 `xcrun llvm-profdata show --binary-ids`；
4. 全部成功后执行 `xcrun llvm-profdata merge -sparse`；
5. 对 merged.profdata 再执行 `llvm-profdata show`；
6. 使用 manifest 的 `coverage_binary` 执行完整 `xcrun llvm-cov export`。

每条命令记录参数、Developer 目录、开始/结束时间、exit code、stdout 和 stderr。当前没有 warning 白名单：exit code 非 0 或任一 stderr 非空都会失败。count 只表达是否执行过，不表达 QA 操作次数。

## input_set_sha256

输入身份使用规范 JSON（UTF-8、键排序、无多余空白）计算 SHA-256。参与字段：

```json
{
  "schema_version": 1,
  "cycle_id": "<cycle_id>",
  "build_id": "<build_id>",
  "manifest_sha256": "<manifest_sha256>",
  "runs": [
    {
      "run_id": "<按 run_id 排序>",
      "payload_size": 123,
      "payload_sha256": "<profraw_sha256>"
    }
  ]
}
```

因此 Run 参数顺序不会改变哈希；Run 集合、任一 profraw 内容或 Build manifest 改变都会产生不同哈希。

## 输出和原子发布

成功目录：

```text
analysis/single-build/<build_id>/<input_set_sha256>/
  inputs.json
  tool_versions.json
  commands.json
  merged.profdata
  llvm-cov-export.json
  summary.json
  completion.json
  <每条命令>.stdout.txt
  <每条命令>.stderr.txt
```

CLI 先写 `analysis/single-build/.staging/`，逐文件 fsync，最后写 `completion.json` 并原子 rename。`completion.json` 保存除自身外每个输出的 size/SHA；幂等复用前会重新校验输入和所有已发布文件。

命令失败证据保存到 `.failed/<build_id>/.../`，其中明确写入 `status=failed` 和错误信息；失败目录不会出现在成功哈希路径下。

## summary.json

摘要记录 Cycle/Build identity、三个 Git SHA、coverage binary、manifest/input 哈希、profraw 数量、合并 profdata 的 size/SHA、LLVM export 格式版本、函数/行/region 总量和 warning。

这是完整 Build 的源码级统计，不是 MR 变更行覆盖率。后台流程会继续把 `llvm-cov-export.json` 与该 Build 固定的 `merge_base_sha...git_sha` Git diff 相交。
