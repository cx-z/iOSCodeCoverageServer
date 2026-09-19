# Runtime root 与凭证部署

服务端从仓库内的 `config/server.json` 读取唯一 runtime root。源码位置只用于定位这份非敏感配置，不用于推导数据目录或项目仓库；当前工作目录不参与运行路径选择。

## 初始化

先创建本机配置。该文件被 Git 忽略，不应提交个人机器路径：

```bash
cp config/server.example.json config/server.json
```

编辑后的内容形如：

```json
{
  "schema_version": 1,
  "runtime_root": "/absolute/coverage-runtime"
}
```

`runtime_root` 必须是源码仓之外的绝对目录。配置缺失、为空、包含未知字段、版本错误或路径非法时，服务不会启动，也不会创建 Token、数据库或日志。

然后准备注册表：

```bash
mkdir -p /absolute/coverage-runtime/config
cp config/projects.example.json /absolute/coverage-runtime/config/projects.json
```

编辑 runtime root 中的 `projects.json` 后再启动。注册表中的 repository 必须是已经存在的 Git worktree 根；完整规则见 [ProjectRegistry](project_registry.md)。仓库内示例只使用虚构身份，真实注册表不得复制回源码目录。新部署使用独立数据库，不从其他服务复制 SQLite、Build/Run 文件或分析目录。

固定布局：

```text
<runtime_root>/config/projects.json
<runtime_root>/state/gitlab_api_token
<runtime_root>/state/operator_token
<runtime_root>/state/packaging_token
<runtime_root>/state/run_upload_token
<runtime_root>/instance/coverage.sqlite3
<runtime_root>/data/
<runtime_root>/analysis/
<runtime_root>/logs/server.log
```

## 启停

```bash
./start_coverage_server.sh
# 或
python3 server.py
```

启动脚本固定监听 `0.0.0.0:8765`，终端中按 `Control-C` 可安全停止。需要诊断参数时使用 `python3 server.py --help`；四个低频分析 CLI 仍需显式传同一个 `--runtime-root`，避免改变其机器可调用接口。

## 凭证

浏览器使用平台账号的 HttpOnly Session，不读取机器 Token。`operator_token` 只保留完整管理员兼容能力；`packaging_token` 只允许已知 Cycle GET/PATCH 与 Build POST/GET；`run_upload_token` 只允许 probe 和 Run 上传；`gitlab_api_token` 只用于注册表内 GitLab 项目的 MR GET。原始 Session 与任何 Token都不得进入日志、HTML、SQLite 明文字段、Build manifest 或分析结果。

operator、packaging 和 Run Token在首次启动时安全生成，之后跨重启复用，权限为 `0600`，且必须两两不同。GitLab Token不自动生成；缺失或失效只会使 MR URL查询失败，不影响服务启动、已有 Run上传与报告读取。首次部署用 `python3 manage_users.py create-admin --username <name>` 隐藏输入初始管理员密码。部署者写入 GitLab Token后应确保文件权限为 `0600`，且不要用会把内容写到终端或日志的诊断命令查看它。

## 路径约束

`RuntimePaths` 是服务端、后台分析器和诊断 CLI 的唯一运行路径事实源。runtime root 会规范化为绝对路径，不能是文件系统根；HTTP 和 CLI 都不能分别覆盖数据库、data、analysis、logs、Token 或项目仓库位置。

运行目录可能包含私有仓库路径、内部项目名和设备上传数据，因此必须保持在源码仓库外。仓库根的 `.gitignore` 也防护误放的 `runtime/`、`state/`、`instance/`、`data/`、`analysis/`、`logs/`、Token、SQLite、profile 和日志文件；发布前还应运行 `python3 scripts/check_public_repository.py`。
