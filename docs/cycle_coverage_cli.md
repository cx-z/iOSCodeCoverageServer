# 跨 Build 累计覆盖 CLI

实现位于 `ioscoverage/analysis/cumulative/`，根命令只是兼容入口；代码导航见 [Analysis 模块](../knowledge/modules/analysis.md)。

`analyze_cycle_coverage.py` 是受限、只读、离线的 Cycle 累计分析器。它逐 Build 消费已经完成的 `llvm-cov-export.json`，把可证明仍未改变的历史 region 命中映射到 current Build，再按 current Build 固定 MR diff 输出新侧变更行结论。它不运行 LLVM、不合并不同 Build 的 profile，也不修改 SQLite、Build 或 Run。

## 调用方式

```bash
cd /path/to/ios-coverage-server
python3 analyze_cycle_coverage.py \
  --runtime-root /absolute/runtime \
  --cycle-id <cycle_id> \
  --current-build-id <current_build_id> \
  --build-input <build_a_id>=<build_a_input_set_sha256> \
  --build-input <current_build_id>=<current_input_set_sha256>
```

`--build-input` 至少一个、可重复，但 Build 不得重复并且必须包含 current Build。所有 Build 必须属于指定 Cycle，注册时间不得晚于 current Build。CLI 不接受文件路径、Git SHA、分支名、仓库路径、LLVM 路径、结果目录或排除规则。

固定解析位置：

```text
SQLite:            instance/coverage.sqlite3
Build 数据:        data/cycles/<cycle_id>/builds/<build_id>
单 Build 输入:     analysis/single-build/<build_id>/<input_set_sha256>
项目 Git 仓库:     由 Cycle.project 在 ProjectRegistry 中解析
累计输出:          analysis/cycle-coverage/<cycle_id>/<current_build_id>/<cycle_input_set_sha256>
```

退出码：`0` 成功或幂等复用，`2` CLI Build identity 格式/重复错误，`3` 输入、Git 或映射语义校验失败，`4` Git 命令失败或 stderr 非空，`5` 证据发布或既有结果完整性失败。

## 输入完整性与固定分母

每个 Build 均重新校验 Cycle 归属、`artifact_state=ready`、manifest/SQLite/实际产物 size/SHA/UUID 一致，并校验单 Build `completion.json` 中全部输出的 size/SHA。`inputs.json`、`summary.json` 的 Cycle、Build、`git_sha`、`target_sha_at_build`、`merge_base_sha`、manifest SHA 和 input set 必须与当前持久数据完全一致。

每个 Build 的三个 commit object 和 merge-base 都会用固定 Git 参数数组验证，并确认配置路径就是固定 Git 工作树根。分析器不读取 HEAD、分支名或工作树源码；HEAD 可以位于任意提交，工作树可以有未提交修改，结论仍只由精确 Git object 决定。分析器不执行 fetch、checkout、switch、reset、clean、stash 或 worktree 操作。

报告分母唯一来自 current Build：

```text
git diff <current.merge_base_sha> <current.git_sha>
```

不读取 master、origin/master、当前目标分支 tip 或 Cycle.base_sha。目标分支后续推进不会改变已经发布的累计结果。

## region 累计与修改失效

每个 Build 的 export 分别解析，不跨 Build 合并 `.profraw`/`.profdata`。当前 MR 候选仍按单 Build diff 的新侧行规则生成：删除退出分母，纯 rename 无行，二进制不制造可测量行，coverage 路径必须精确映射到固定仓库相对路径。

对每个 current region：

1. current count > 0 时直接命中，历史结果不能降级它；
2. current count = 0 时，按注册时间从新到旧检查历史 Build；
3. Git diff 必须为精确同路径，或唯一、明确的 rename；
4. zero-context hunks建立 current→historical 行映射，region 涉及的每一行都必须位于 Git 声明的字节完全相同区块，且映射连续；
5. current/historical region 的起止列、映射后的起止行、源码范围 SHA-256 和规范化函数身份必须完全匹配；
6. 唯一历史 region count > 0 时才继承，并记录 Build/input/git/path/range/hash；历史 count=0 或找不到兼容 region 均不继承。

任一 region 行发生修改（第一版包括空白变化）、新增 region、复制到另一位置、同 basename 不同目录、相同文本出现在其他位置、歧义 rename、冲突 coverage 或不连续映射，都不能猜测为历史命中。跨多行 region 的任一行修改会使整个旧 region 失效。“未找到可继承历史 region”不等于 `unknown`；只要 current region 本身合法，它保持 `uncovered`。

每条 current 新侧变更行：

- `covered`：所有相关可测量 current regions 均由 current Build 或至少一个兼容历史 Build 提供布尔命中；
- `uncovered`：至少一个相关 current region 没有命中证据；
- `not_measurable`：Git 行/源码确定，但 current export 没有相关可测量非 gap region；
- `unknown`：current coverage 路径冲突、region 损坏/越界或当前结论无法可靠生成。

只有 `covered + uncovered` 进入分母。count > 0 只表示执行过，不表示 QA 次数；行级结论不是 Token 级语义测试证明。

## 输出和身份

成功目录包含：

```text
manifest.json
cumulative-line-coverage.json
mapping-evidence.json
summary.json
commands.json
git-*.stdout.txt / git-*.stderr.txt
git-*-diff.patch
complete.json
```

`cumulative-line-coverage.json` 每行保存 Git 身份、源码文本/hash、状态及相关 regions。每个 region 的 `source.kind` 为 `current_build` 或 `inherited_build`；继承来源包含 build/input/git SHA、历史/current 源码范围、映射原因和源码范围 SHA-256。`mapping-evidence.json` 保存所有未命中尝试，因此可以审计“为何继承”与“为何失效”。

`cycle_input_set_sha256` 对规范 JSON 计算 SHA-256，包含：

- schema/analyzer 版本；
- Cycle 和 current Build identity；
- current Build 固定 `target_sha_at_build/merge_base_sha/git_sha`；
- 按 `created_at + build_id` 稳定排序的全部 Build identity；
- 各 Build manifest SHA、input set SHA、LLVM export SHA；
- current denominator 的规范 Git diff SHA；
- 历史 old→current 规范 Git diff、region 范围和源码片段哈希组成的 mapping evidence SHA。

调用参数顺序不影响 identity。结果先写同文件系统 staging，逐文件 fsync 后原子 rename；`complete.json` 哈希其他全部输出。相同输入只在既有结果完整性复核通过后返回 `idempotent_replay=true`；失败进入 `.failed`，不会产生成功标记。

日常后台分析通过只接受 `cycle_id` 的一键编排器调用本分析器。手工指定 Build/input 的入口保留用于精确复核，见 [一键 Cycle CLI](cycle_latest_cli.md)。
