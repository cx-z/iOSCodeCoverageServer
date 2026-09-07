# iOS Coverage Platform Server

本仓库是独立的 Mac 本机 iOS 真机增量覆盖率服务端。它接收已注册 Build 的 LLVM 产物和用户主动上传的 `.profraw`，按 Build 独立分析，再对当前 Build 的固定 Git diff 做严格的源码 region 累计。

## 部署

源码目录不保存运行数据。首次使用只需配置一次运行数据路径，再准备项目注册表：

```bash
cp config/server.example.json config/server.json
# 编辑 config/server.json，将 runtime_root 改为一个绝对数据目录。

mkdir -p <runtime_root>/config
cp config/projects.example.json <runtime_root>/config/projects.json

./start_coverage_server.sh
```

`config/server.json` 是本机配置且被 Git 忽略；`runtime_root` 为空、缺失或不是绝对路径时，服务会在创建 Token、数据库和日志前停止并提示配置。仓库中的 `projects.example.json` 只含虚构项目；真实项目注册表必须仅放在 `<runtime_root>/config/projects.json`。

所有可变数据只位于该 runtime root：

```text
config/projects.json
state/gitlab_api_token
state/operator_token
state/run_upload_token
instance/coverage.sqlite3
data/
analysis/
logs/server.log
```

操作员与 Run Token 缺失时自动生成并设置 `0600`；GitLab Token 必须由操作员单独写入，缺失只影响 MR URL 创建 Cycle。

浏览器入口：`http://127.0.0.1:8765/cycles/`。HTTP 协议见 [docs/api.md](docs/api.md)，日常流程见 [docs/daily_workflow.md](docs/daily_workflow.md)，部署配置见 [docs/runtime_deployment.md](docs/runtime_deployment.md) 和 [docs/project_registry.md](docs/project_registry.md)。

## 诊断 CLI

四个兼容入口都必须显式传 `--runtime-root`，且只接受 Cycle/Build/Run 等不可变身份，不能传仓库或产物路径：

```bash
python3 analyze_cycle_latest.py --runtime-root /absolute/runtime --cycle-id <cycle-id>
```

服务正常运行时，Run 上传会异步触发同一编排器，不需要人工运行 CLI。

## 开发验证

```bash
PYTHONPYCACHEPREFIX=/private/tmp/ios-coverage-tests-pycache \
python3 -m unittest discover -s tests -t . -v
```

工程导航从 [knowledge/README.md](knowledge/README.md) 开始。服务端 Python 包名为 `ioscoverage`。

## 公开仓库检查

提交或发布前运行：

```bash
python3 scripts/check_public_repository.py
```

检查器扫描 Git 已跟踪文件以及未被 `.gitignore` 排除的未跟踪文件，拒绝内部项目标识、本机用户目录、运行产物和常见明文凭证特征。它只输出相对文件路径和安全的命中类型，不输出疑似凭证内容。真实 `projects.json`、Token、SQLite、profile、日志和分析结果都是部署数据，不应进入源码仓库。

## License

This project is licensed under the [Apache License 2.0](LICENSE).
