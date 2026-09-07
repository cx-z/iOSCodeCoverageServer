# 单 Build MR 变更行覆盖分析 CLI

实现位于 `ioscoverage/analysis/diff/`，根命令只是兼容入口；代码导航见 [Analysis 模块](../knowledge/modules/analysis.md)。

`analyze_single_build_diff.py` 只消费一个已完成、完整性校验通过的单 Build `llvm-cov-export.json`，并与该 Build 固定的 Git 比较快照相交。它不重新运行 LLVM，不修改 SQLite、服务端数据或 Git，不实现跨 Build 累计、HTTP、UI 或 GitLab。

## 调用方式与固定边界

```bash
cd /path/to/ios-coverage-server
python3 analyze_single_build_diff.py \
  --runtime-root /absolute/runtime \
  --cycle-id <cycle_id> \
  --build-id <build_id> \
  --input-set-sha256 <input_set_sha256>
```

仅接受上述三个身份参数。不能指定 merge base/head、分支名、Git 仓库、coverage JSON、源码、diff 或输出目录。固定路径为：

```text
SQLite:          instance/coverage.sqlite3
Build 数据:      data/cycles/<cycle_id>/builds/<build_id>
单 Build 输入:   analysis/single-build/<build_id>/<input_set_sha256>
项目注册仓库:         由 Cycle.project 在 ProjectRegistry 中解析
diff 输出:       analysis/single-build-diff/<build_id>/<input_set_sha256>/<diff_set_sha256>
```

退出码：`0` 成功或幂等复用，`2` 参数解析错误，`3` 输入/Git/coverage 语义校验失败，`4` Git 命令失败或产生 stderr，`5` 证据存储或既有结果完整性失败。

## 比较快照和只读校验

分析器从只读 SQLite 和 Build manifest 取得 `target_sha_at_build`、`merge_base_sha`、`git_sha`。报告范围固定为：

```text
git diff <merge_base_sha> <git_sha>
```

它等价于 Build 构建时的 `git diff <target_sha_at_build>...<git_sha>`，但不读取当前 master、origin/master、Cycle.base_sha、HEAD 或工作树源码。开始 diff 前会验证配置路径就是固定 Git 工作树根、三个 commit object 存在，并确认 `git merge-base target_sha_at_build git_sha`。HEAD 可以位于其他提交，工作树也可以有未提交修改；不会 fetch、checkout、switch、reset、clean、stash 或创建 worktree。

Git 命令均使用参数数组且不经过 shell。审计证据包括：

- `git diff --name-status -z --find-renames --no-ext-diff --no-textconv`；
- `git diff --unified=0 --no-color --no-ext-diff --no-textconv --find-renames`；
- 对每个含新侧文本变更的文件执行 `git show <git_sha>:<repo_relative_path>`。

状态清单使用 NUL 分隔，支持空格路径。路径必须是无 `..` 的规范仓库相对路径；源码只读 Git object，不读取工作树同名文件。纯 rename 没有变更行，rename 后修改使用新路径/新行号；删除行只进入 `deleted_excluded`，二进制文件不制造虚假源码行。

在任何 Git 命令前，分析器还会校验 Build ready、manifest/SQLite/产物 size/SHA/UUID 描述一致，以及单 Build `completion.json` 中全部文件 size/SHA 未被篡改；`inputs.json`、`summary.json` 的 Build 身份必须完全一致。

## coverage 路径与 region 规则

当前支持 Xcode 26.3 实际输出的 `llvm.coverage.json.export` 2.0.1。编译绝对路径只能严格映射到注册项目仓库内的相对路径，再与 Git 新路径精确匹配；不使用 basename、后缀或模糊回退。仓库外 coverage 文件不会成为 Git 候选；同一路径冲突记录、损坏或越界 region 标为 `unknown`，不猜测。

可测量区域只使用 LLVM code region（kind 0）及其明确 count，排除 skipped/gap。Xcode 26.3 会输出合法的零长度 code region；它没有源码交集面积，因此被忽略而不污染文件。每条新侧行按所有相交区域分类：

- `covered`：至少一个可测量 region，且所有相关 count 都大于 0；
- `uncovered`：至少一个可测量 region，且任一相关 count 为 0；正数与 0 混合仍保守归入此类；
- `not_measurable`：Git 行和源码可精确确认，但没有可测量非 gap region；
- `unknown`：路径冲突、region 损坏/越界或其他无法可靠对应的情况。

只有 `covered + uncovered` 进入分母：

```text
changed_line_coverage_percent = covered / (covered + uncovered) * 100
```

分母为 0 时百分比为 `null`。count 大于 0 只表示 region 执行过，不表示 QA 操作次数。行状态是 compiler region 与 Git 新侧行的交集，不证明方法名、变量名或该行每个 Token 的语义都经过测试。

## diff_set_sha256 与输出

`diff_set_sha256` 对规范 JSON 计算 SHA-256，至少包含 schema、Cycle/Build、manifest SHA、input set SHA、export SHA、固定 merge base/head、规范化 Git diff SHA 和分析器版本。Git patch 的非语义格式差异不进入规范 diff 身份；变更内容或 export 内容变化会生成新身份。

成功目录至少包含：

```text
inputs.json
git-files.json
git-diff.patch
normalized-diff.json
normalized-coverage.json
changed-line-coverage.json
summary.json
commands.json
<每条 Git 命令>.stdout.txt
<每条 Git 命令>.stderr.txt
completion.json
```

`changed-line-coverage.json` 为每条新侧变更行保存路径、状态、旧/新路径、行号、Git object 源码文本及 SHA-256、分类原因和相关 region 起止位置/count。`summary.json` 同时提供整体和逐文件的 covered、uncovered、not_measurable、unknown、deleted_excluded。

全部文件先写 `.staging`，fsync 后原子发布。`completion.json` 保存其他输出的 size/SHA；相同输入只有在既有结果重新校验通过时才幂等复用。失败证据写入 `.failed`，不会伪装为完成结果。
