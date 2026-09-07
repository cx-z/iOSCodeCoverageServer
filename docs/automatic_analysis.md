# 上传后后台自动分析

代码定位见 [自动分析工作流](../knowledge/workflows/automatic_analysis.md)；本文只维护持久 generation 与调度行为。

iPhone 只在用户点击“上传覆盖数据”时联网。新 Run 的文件和 SQLite 记录完整提交后，HTTP 按 Run 协议立即返回；Mac 的全局单 worker 在后台调用同一个 `CycleAnalysisOrchestrator` 更新累计报告。

## 持久状态

SQLite `cycle_analysis_states` 每个 Cycle 最多一行，保存：

- generation：`requested_generation`、`completed_generation`、`running_generation`；
- 状态：`idle`、`queued`、`running`、`succeeded`、`failed`；
- 请求、开始、完成时间，attempt/recovery 计数和触发来源；
- 本次结果的 orchestration、Cycle input、current Build 和内部结果目录；
- latest success 的 generation、identity、小型摘要和不可变结果引用；
- 失败 stage、稳定 code 和长度受限的安全 message。

SQLite 不保存 profraw、profdata、LLVM export、逐行 coverage 或命令输出；这些继续保存在 `analysis/` 的不可变目录。

## Generation 与调度

1. 每个有效触发使 `requested_generation += 1`。
2. idle/succeeded/failed 进入 queued；queued 只更新最新 generation；running 保持运行并记录更高 generation。
3. 全局 worker 每次只 claim 一个 Cycle 的最新 generation，并在锁外执行分析。
4. 完成时使用 `status=running AND running_generation=<generation>` 条件提交，旧任务不能覆盖新 generation。
5. 如果 requested generation 已前进，当前任务完成后回到 queued，只再分析一次最新一致快照。
6. 同一输入的 retry 仍计一次 attempt，但编排器会在完整性复核后幂等复用不可变结果。

调度器不在 Condition 锁内执行 SQLite、LLVM、Git、文件 I/O 或外部回调。SQLite connection 不跨线程共享。不同 Cycle 也由同一个 worker 串行分析。

## 自动触发边界

触发分析：

- 新 Run 已原子持久化，且为 `uploaded/analyzable`、无错误；
- 新 Build 注册并实际把至少一条 `pending_build` Run 提升为 analyzable；
- 人工 retry。

不触发分析：

- 鉴权、multipart、size、SHA、metadata 或身份校验失败；
- Run 仍为 `pending_build`；
- Run 或 Build 的完全幂等重放；
- 网络探针、health、GET 或普通 Cycle 查询。

历史 Build 的新有效 Run 也会触发 Cycle，因为它可能增加 current Build 报告的历史继承证据。调度发生在上传/注册事务和文件原子发布成功之后；调度请求本身失败只写服务日志，不回滚已经成功的 Run/Build。

## HTTP 协议

两个接口均要求 `Authorization: Bearer <token>`：

```text
GET  /api/v1/cycles/<cycle_id>/analysis
POST /api/v1/cycles/<cycle_id>/analysis/retry
```

从未调度的已知 Cycle 返回：

```json
{
  "cycle_id": "<cycle_id>",
  "status": "idle",
  "requested_generation": 0,
  "completed_generation": 0,
  "attempt_count": 0,
  "last_trigger": null,
  "latest_run_id": null,
  "current_build_id": null,
  "orchestration_input_sha256": null,
  "cycle_input_set_sha256": null,
  "latest_success": null,
  "error": null
}
```

queued、running、failed 仍返回 `latest_success`；failed 的 `error` 为 `{stage, code, message}`。响应不暴露 Token、内部输出目录、Mach-O、dSYM、命令或环境。

retry 不接受请求体，也不接受 Build、Run、Git、路径或命令参数。成功返回 HTTP 202 和入队后的状态；queued/running 时只合并成更高 generation。未知 Cycle 返回 HTTP 404 `cycle_not_found`。

示例：

```bash
TOKEN="$(cat <runtime_root>/state/operator_token)"

curl --fail-with-body \
  -H "Authorization: Bearer ${TOKEN}" \
  http://127.0.0.1:8765/api/v1/cycles/<cycle_id>/analysis

curl --fail-with-body -X POST \
  -H "Authorization: Bearer ${TOKEN}" \
  http://127.0.0.1:8765/api/v1/cycles/<cycle_id>/analysis/retry
```

## 失败、重启与关闭

- 确定性或未知分析失败只执行一次并进入 failed；不修改 Run，不清除 latest success。
- 新有效 Run 或人工 retry 可以从 failed 请求新 generation。
- 服务启动把遗留 running 恢复为 queued、清除 running generation 并增加 recovery count；恢复本身不增加 generation，也不循环重试。
- 服务停止先拒绝新调度并通知 worker，最多等待 30 秒；进程意外中断后由下次启动恢复。
- 分析器只读取固定 Git objects、登记产物和快照 Run，不操作工作树。

页面每 2 秒读取本接口，并从独立 Bearer 保护的报告接口获取 latest success。页面刷新不会修改 generation，也不会让 Run 上传等待分析。编排器按 Cycle.project 从 ProjectRegistry 选择仓库。日常流程见 [iOS 真机覆盖率日常使用](daily_workflow.md)。
