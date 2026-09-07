# iOS 真机增量覆盖率日常使用

## 启动

Mac 与设备在同一可信局域网且关闭 VPN。服务源码与运行数据分离：

```bash
cd /path/to/ios-coverage-server
./start_coverage_server.sh
```

启动脚本从仓库内、不含凭证的 `config/server.json` 读取 `runtime_root`。首次使用先复制 `config/server.example.json` 并填写绝对路径；空值会阻止启动并显示配置提示。保持终端运行；停止时按 `Control-C`。浏览器打开 `http://127.0.0.1:8765/cycles/`，输入 `<runtime_root>/state/operator_token`。受限 iOS 凭证位于 `<runtime_root>/state/run_upload_token`；GitLab PAT 位于 `<runtime_root>/state/gitlab_api_token`。真实项目配置和全部运行数据只保存在 runtime root，不复制进源码仓库。

## Cycle 与测试包

在 `/cycles/` 粘贴已在 `config/projects.json` 注册的 GitLab MR URL。服务端只做固定 MR GET，创建或复用 `project + mr_iid` 对应 Cycle。列表可按项目筛选，并显示项目名称。

打包工具仍提交原 schema v1 Cycle/Build/Run 协议。Build manifest 不提交仓库路径或项目路径；服务端通过 Cycle.project 自动选择注册仓库，并校验 Bundle ID 白名单、Git object、merge-base、Mach-O/dSYM UUID、size 和 SHA。

## QA 上传与后台分析

App 自动累计并保存 profraw，但不会在启动、前后台或定时检查时联网。QA 覆盖安装、执行测试后，只点击一次“上传覆盖数据”。Run 成功持久化后 HTTP 立即返回，后台分析失败不会撤销 Run，也不会删除上一次成功报告。

不同 Build 的 profile 始终分别生成 LLVM export；Cycle 累计只在可证明未修改的源码 region 层继承，绝不跨 Build 合并 profraw/profdata。

## 报告

页面每两秒刷新 queued/running/succeeded/failed 状态，展示 covered、uncovered、not measurable、unknown、当前 Build 与历史继承来源。分母固定为 current Build 的 `merge_base_sha -> git_sha`；目标分支后续变化不会重写报告。

故障诊断先检查 `<runtime_root>/logs/server.log`，不要输出 Token。需要人工只读重跑时使用四个诊断 CLI，并显式传 `--runtime-root`；CLI 不接受仓库、GitLab、Mach-O、profile 或输出路径。
