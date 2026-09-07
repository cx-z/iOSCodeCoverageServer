# 一键 Cycle 离线分析 CLI

实现位于 `ioscoverage/analysis/orchestrator.py`，根命令只是兼容入口；代码导航见 [自动分析工作流](../knowledge/workflows/automatic_analysis.md)。

`analyze_cycle_latest.py` 是只读、可审计的一键编排入口。人工调用时只提供一个已登记的 `cycle_id`；编排器自动冻结输入、选择 current Build、选择每个 Build 的全部有效 Run，并依次调用既有单 Build LLVM、current MR diff 和跨 Build累计分析器。

后台 worker 直接调用同一个 `CycleAnalysisOrchestrator` 类，HTTP 层不通过 shell 调 CLI，也不复制 LLVM/diff/累计逻辑。本文件描述的快照、选择、identity 和不可变发布规则对人工 CLI 与后台执行完全相同。后台 generation、状态查询和 retry 见 [后台自动分析](automatic_analysis.md)。只读页面只消费 completion 保护的结果；系统没有可变 `latest.json`。本 CLI 不直接调用 GitLab；MR 元数据由 Cycle HTTP 创建流程读取。

## 调用方式

```bash
cd /path/to/ios-coverage-server
python3 analyze_cycle_latest.py \
  --runtime-root /absolute/runtime \
  --cycle-id <cycle_id>
```

唯一参数是 `--cycle-id`。不能传 current/build/run/input set、Git SHA/分支、仓库、LLVM、源码或输出路径，未知参数由 `argparse` 以退出码 2 拒绝。

## 一致快照与自动选择

编排器使用 SQLite URI `mode=ro`、`PRAGMA query_only=ON` 和显式只读事务，一次读取 Cycle、全部 Build 与相关 Run。之后只把快照内明确的 Run ID 传给单 Build分析器；分析过程中新增的 Run 不会混入本次结果，下次调用会形成新的编排身份。

选择规则：

1. current Build 必须 `artifact_state=ready` 且 `git_sha == Cycle.head_sha`；有多个同提交 Build 时，以 `(created_at, build_id)` 最大者稳定选为 current；没有匹配项时失败。
2. 历史输入必须属于同一 Cycle、ready、`created_at` 不晚于 current，并至少有一条 `state=uploaded`、`analyzable=true`、`error_message` 为空的 Run。
3. 每个入选 Build 使用快照内全部有效 Run，按 `run_id` 排序；任一选中 Run 的 metadata、profraw size/SHA 损坏会使整个编排失败，不会被跳过。
4. 无有效 Run 的 Build 记录 `skip_reason=no_analyzable_runs`；invalid 或晚于 current 的 Build 也记录明确原因。
5. 每个 Build 独立运行单 Build LLVM，不跨 Build 合并 `.profraw` 或 `.profdata`；current diff 与累计算法仍由既有分析器负责。

## Git 与工作树边界

仓库由 `Cycle.project -> ProjectRegistry` 固定解析，CLI 不能覆盖。底层分析器验证该项目仓库根、每个 Build 的三个 commit object 和精确 merge-base。LLVM 只读取登记的 Mach-O、dSYM 和 profraw；diff 使用固定 SHA；源码只通过：

```text
git show <commit_sha>:<repo_relative_path>
```

读取。coverage 绝对路径只用于严格映射到固定仓库相对路径，不读取当前文件内容，不使用 basename fallback。

因此不再要求 HEAD 等于历史 Build，也不要求工作树干净。当前分支、HEAD、同路径未提交文件都不进入 identity 或 coverage 结论。分析器不会执行 checkout、switch、reset、clean、stash、merge、rebase 或 worktree 操作。

## orchestration_input_sha256

编排身份对规范 JSON 计算 SHA-256，至少包含：

- schema/orchestrator 版本；
- Cycle 的 ID、base/head、状态与持久时间身份；
- current Build 身份与选择规则；
- 全部入选 Build 的三个比较 SHA、coverage binary、manifest SHA、登记产物 size/SHA；
- 各 Build 快照内全部有效 Run 的 ID、metadata SHA、profraw size/SHA；
- 被跳过 Build 的身份和原因；
- Build/Run 固定排序规则。

相同快照得到相同身份。新增有效 Run 会产生新的编排身份，并由相关 Build 产生新的 input set；旧结果不会被覆盖。

## 输出、引用与失败

成功目录：

```text
analysis/cycle-latest/<cycle_id>/<orchestration_input_sha256>/
  manifest.json
  selected-builds.json
  selected-runs.json
  results.json
  summary.json
  complete.json
```

这里只保存编排证据，不复制 profraw、profdata、LLVM export、Build 或 dSYM。`results.json` 记录每个 Build input、single-Build completion/summary SHA、current diff、final Cycle identity、绝对结果目录及对应完成文件 SHA。幂等复用会同时校验编排目录和全部下游引用；任何字节被修改都会拒绝复用。

失败阶段稳定为 `snapshot`、`build_selection`、`run_validation`、`single_build`、`current_diff`、`cumulative` 或 `publication`。失败证据进入 `analysis/cycle-latest/.failed/<cycle_id>/.../failure.json`，不会出现 `complete.json`，也不会把部分结果冒充最新完整报告。
