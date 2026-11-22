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
# 关闭后台服务
./stop_coverage_server.sh
# 或直接以前台方式运行（跳过重新编译和重启包装）
python3 server.py
```

启动脚本先强制重新编译 Python 源码并检查 `server.log` 与 `server-console.log` 均可追加写入，成功后优雅关闭本 checkout 已运行的服务，再通过独立进程会话（`start_new_session=True`）后台启动当前代码，固定监听 `0.0.0.0:8765`。Python 服务不需要 Xcode 编译。脚本最多等待 60 秒确认新进程监听端口且绕过代理的本机 `/health` 成功，然后返回命令行，不需保留终端；关闭终端也不会向服务传递挂断信号。编译、配置或日志文件打开失败不会关闭旧服务。

结构化日志继续写入 `<runtime_root>/logs/server.log`；启动输出、标准输出/错误及未捕获异常追加到 `<runtime_root>/logs/server-console.log`，不持续输出到启动终端。启动失败会给出控制台日志路径；如初始化清理超过就绪等待期限，脚本返回错误并提示仍在后台的 PID，不会强行中断初始化，需查看日志确认进度。

运行 `./stop_coverage_server.sh` 关闭后台服务。关闭脚本读取 macOS 原生参数边界验证进程属于当前 checkout，拒绝附着式执行参数和不支持的解释器选项，发送 SIGTERM 并最多等待 60 秒；超时返回错误，不发送 SIGKILL、不删除运行锁。端口被其他程序占用会拒绝操作。未运行时重复关闭可安全成功，非 8765 端口的诊断服务不在这两个脚本的管理范围内。启动和关闭期间 TERM/INT 只记录退出请求，启动清理中的受保护删除完成后再关闭资源并释放运行锁；就绪超时后仍可安全请求关闭，关闭超时不会强杀。进程检查命令有独立超时，中文终端不会改变日期解析。直接运行 `python3 server.py` 仍为前台诊断入口，可按 `Control-C` 停止。

需要诊断参数时使用 `python3 server.py --help`；四个低频分析 CLI 仍需显式传同一个 `--runtime-root`，避免改变其机器可调用接口。

## 凭证

浏览器使用平台账号的 HttpOnly Session，不读取机器 Token。`operator_token` 只保留完整管理员兼容能力；`packaging_token` 只允许已知 Cycle GET/PATCH 与 Build POST/GET；`run_upload_token` 只允许 probe 和 Run 上传；`gitlab_api_token` 只用于注册表内 GitLab 项目的 MR GET。原始 Session 与任何 Token都不得进入日志、HTML、SQLite 明文字段、Build manifest 或分析结果。

operator、packaging 和 Run Token在首次启动时安全生成，之后跨重启复用，权限为 `0600`，且必须两两不同。GitLab Token不自动生成；缺失或失效只会使 MR URL查询失败，不影响服务启动、已有 Run上传与报告读取。首次部署用 `python3 manage_users.py create-admin --username <name>` 隐藏输入初始管理员密码。部署者写入 GitLab Token后应确保文件权限为 `0600`，且不要用会把内容写到终端或日志的诊断命令查看它。

## 路径约束

`RuntimePaths` 是服务端、后台分析器和诊断 CLI 的唯一运行路径事实源。runtime root 会规范化为绝对路径，不能是文件系统根；HTTP 和 CLI 都不能分别覆盖数据库、data、analysis、logs、Token 或项目仓库位置。

运行目录可能包含私有仓库路径、内部项目名和设备上传数据，因此必须保持在源码仓库外。仓库根的 `.gitignore` 也防护误放的 `runtime/`、`state/`、`instance/`、`data/`、`analysis/`、`logs/`、Token、SQLite、profile 和日志文件；发布前还应运行 `python3 scripts/check_public_repository.py`。
