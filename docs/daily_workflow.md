# iOS 真机增量覆盖率日常使用

## 启动

Mac 与设备在同一可信局域网且关闭 VPN。服务源码与运行数据分离：

```bash
cd /path/to/ios-coverage-server
./start_coverage_server.sh
```

启动脚本从仓库内、不含凭证的 `config/server.json` 读取 `runtime_root`。首次使用先复制 `config/server.example.json` 并填写绝对路径；空值会阻止启动并显示配置提示。首次部署在服务停止时创建管理员：

```bash
python3 manage_users.py create-admin --username <admin-name>
```

命令通过终端两次隐藏输入密码，不接受密码参数，也不输出密码或摘要。保持服务终端运行；停止时按 `Control-C`。浏览器打开 `http://127.0.0.1:8765/login/`：默认显示登录，首次使用可从表单下方切换为注册；注册成功会自动登录并进入 `/cycles/`，无需再次输入密码。已有有效会话访问登录页会直接进入工作台；未登录访问 `/cycles/` 会跳回登录页。packaging 凭证位于 `<runtime_root>/state/packaging_token`，受限 iOS 凭证位于 `<runtime_root>/state/run_upload_token`，GitLab PAT 位于 `<runtime_root>/state/gitlab_api_token`；operator Token只保留管理员兼容。真实项目配置和全部运行数据只保存在 runtime root，不复制进源码仓库。

## Cycle 与测试包

在 `/cycles/` 粘贴已在 `config/projects.json` 注册的 GitLab MR URL。服务端只做固定 MR GET，创建或复用 `project + mr_iid` 对应 Cycle，并记录当前平台账号的提交关系。developer 列表只包含自己的提交，admin 列表包含全部 Cycle和提交账号。

复制页面显示的 `cycle_id`，在宿主 Coverage 分支用 `COVERAGE_PACKAGING_TOKEN_FILE` 和 `COVERAGE_RUN_UPLOAD_TOKEN_FILE` 执行宿主打包薄脚本的 `-D -C <cycle_id>` 模式。普通开发者不需要 operator Token。packaging Token只能查询/更新已知 Cycle和注册/复核 Build。

打包工具仍提交原 schema v1 Cycle/Build/Run 协议。Build manifest 不提交仓库路径或项目路径；服务端通过 Cycle.project 自动选择注册仓库，并校验 Bundle ID 白名单、Git object、merge-base、Mach-O/dSYM UUID、size 和 SHA。

## QA 上传与后台分析

App 自动累计并保存 profraw，但不会在启动、前后台或定时检查时联网。QA 覆盖安装、执行测试后，只点击一次“上传覆盖数据”。Run 成功持久化后 HTTP 立即返回，后台分析失败不会撤销 Run，也不会删除上一次成功报告。

不同 Build 的 profile 始终分别生成 LLVM export；Cycle 累计只在可证明未修改的源码 region 层继承，绝不跨 Build 合并 profraw/profdata。

## 报告

页面每两秒刷新 queued/running/succeeded/failed 状态，展示 covered、uncovered、not measurable、unknown、当前 Build 与历史继承来源。分母固定为 current Build 的 `merge_base_sha -> git_sha`；目标分支后续变化不会重写报告。

如果旧版本曾因打包机与服务端源码根不同而把整包标为不可测，部署修复后的 Server 后，由管理员在该 Cycle 页面或按 [后台自动分析](automatic_analysis.md#http-协议) 的无请求体 retry 接口人工重分析。先确认当前 Build 与 Cycle head 匹配、原始或保留的分析输入仍完整，再查看新 generation 是否 `succeeded`、可测行是否恢复。失败时记录安全错误码并检查项目注册及固定 Git 对象；旧 latest success 不会被覆盖。无需重新打包、修改 Build manifest 或重新上传已有 Run。

故障诊断先检查 `<runtime_root>/logs/server.log`，不要输出 Token。需要人工只读重跑时使用四个诊断 CLI，并显式传 `--runtime-root`；CLI 不接受仓库、GitLab、Mach-O、profile 或输出路径。

## 运行数据清理

服务绑定端口后、开始接收请求前会先安全清理一次，之后从本次进程启动时间起每 24 小时再清理一次；重启服务会重新计时。清理会等待正在执行的分析自然结束，不会停止 HTTP 服务或中断报告读取。

需要立即手动清理时，由 admin 页面 Session或 operator 兼容 Token发送无请求体 POST：

```bash
curl -X POST http://127.0.0.1:8765/api/v1/maintenance/cleanup \
  -H 'Authorization: Bearer <operator-token>'
```

可用同一路径 GET 查看状态。`python3 cleanup_runtime_data.py` 仍只做 dry-run；离线 `--apply` 仅作为服务停止时的备用方式。
