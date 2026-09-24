# DevFlow

[English](README.md) | [简体中文](README.zh-CN.md)

DevFlow 是一个本地优先、人工参与审批的代码变更工作流：导入干净的 Git
快照，请求兼容 Chat Completions 的提供商生成变更，检查隔离的 Python 检查结果并进行审阅，
然后批准并导出补丁。**你的原始检出目录以只读方式挂载；批准不会将变更应用到其中。**

**v0.2.0-beta.1 — 源码分发的本地 beta 版。** 不提供桌面安装程序或预构建的
容器镜像。参见[Beta 设置与限制](docs/BETA.md)、[验证记录](docs/GITHUB_RELEASE.md)
和 [GitHub Prerelease](https://github.com/CalvinL10/devflow/releases/tag/v0.2.0-beta.1)。

**技术栈：** Python 3.11 · FastAPI · LangGraph · SQLite · Next.js · React · Docker

## Windows NTFS 导入限制

**带有合成可执行位的 Windows 驱动器绑定不支持真实项目导入。** 已测试的 `F:`
挂载会使 `/project` 中提交的五个 `100644` fixture 文件全部显示为 `0777`，包括
`fixture.py` 和 `test_fixture.py`。修补后的导入器会正确拒绝这些不匹配。成功启动或较早的
工作流运行不能证明兼容性。不要禁用模式验证，不要信任 `core.fileMode=false`，也不要
通过 chmod 用户源代码来强行接受。提交模式会在导出时保留，不用于掩盖工作树模式问题。

请使用保持模式的新建的 Linux 仓库副本，或使用 WSL 原生 Linux 存储，例如
`/home/<user>/projects`（不要使用 `/mnt/c` 或 `/mnt/f`）。只有在该 Linux 环境中已有
Docker 可用时，才从其中启动 Compose，并在导入前根据提交验证实际 `/project` 模式。
WSL 原生路径已在 Ubuntu 和 Docker Desktop 集成下验证：Git 模式在只读绑定中保持不变，
导入的源代码也未改变。在 Docker Desktop 中为所选发行版启用集成后再启动。
Linux 文件系统语义不要求使用 C: 盘：WSL 虚拟磁盘和 Docker 存储可以位于 D: 或 F:。
该磁盘中的 Linux 路径不同于 `/mnt/f` 下的 NTFS 绑定。启动脚本不会迁移磁盘或修改 Docker 设置。

原生 Windows 导入器可以检查普通的 `100644` 文件，但由于无法验证可执行位，会拒绝已提交的
`100755` 文件。明确的 no-import 演示仍与真实项目导入支持分开。现有 Ubuntu CI 会在导入前
检查 Linux fixture 的挂载模式；这不能证明 Windows NTFS 支持。

## 启动真实模式

安装 Git，以及支持 Linux 容器的 Docker，并确保 Docker Compose 为 **2.24.4 或更高版本**。
使用本地 Docker daemon，不要使用远程 context。端口 127.0.0.1:3000 必须空闲，或选择另一个本地端口。
从 DevFlow 源码目录运行；Compose 路径不需要主机 Python 或 Node 安装。首次构建以及提供商/依赖操作需要网络。

目标必须是普通 Git 仓库根目录，拥有已提交的 HEAD 和干净的工作树（包括未跟踪文件）。
此 beta 版请使用小型 Python 项目；不支持链接工作树、子模块、二进制文件和大型仓库。
启动检查只检查仓库目录和已提交的 HEAD，不检查工作树是否干净。它有意不运行源仓库的
`git status` 或刷新索引：即使禁用了 hooks 和 fsmonitor，clean filters 仍可能执行。
安全的隔离导入预览会决定工作树是否干净，并且必须在导入前接受源代码。

**Windows PowerShell 启动语法**（Docker Desktop Linux-container 模式；上述 NTFS 导入限制仍适用）：

```powershell
.\scripts\devflow.ps1 start -Repository 'C:\projects\my-python-project'
.\scripts\devflow.ps1 status
```

**Linux Bash：**

```bash
bash scripts/devflow.sh start --repository /home/me/projects/my-python-project
bash scripts/devflow.sh status
```

如果 3000 端口被占用，请在 PowerShell 中追加 `-Port 3001`，或在 Bash 中追加 `--port 3001`。
启动器会同时更新 loopback 监听器和允许的 origin；改为打开 `http://127.0.0.1:3001`。
生命周期命令使用相同的端口参数。直接使用 Compose 时设置 `DEVFLOW_PORT=3001`。不会发布后端端口。

打开 **http://127.0.0.1:3000**，不要使用 LAN 地址或其他主机名。

1. 在提供商设置中输入提供商 base URL、模型和 API key。保存并测试连接。真实模式绝不会静默回退到 mock 模式。
2. 刷新项目预览。检查包含/排除的路径并解决错误。选择一个支持的依赖源（或不选）以及可选的额外依赖。
3. 明确同意将列出的源代码分享给已配置的提供商，然后导入。未经授权，不要提交敏感信息或专有代码。
4. 提交任务，检查其时间线、补丁、检查结果和审阅，然后批准或拒绝。已批准的导入运行可以导出补丁；
   请独立审阅，并遵循[手动应用流程](docs/BETA.md#apply-an-exported-patch)。

凭据与数据库/工作区分开存储，不在浏览器存储设置或源仓库中。不要将 API key 放入 `.env`、终端参数、
截图或 issue 报告。提供商使用可能产生费用。

## 明确的确定性演示

演示是单独的 Compose 覆盖配置，不是默认模式，也不能证明真实模型质量。它不需要源仓库挂载或提供商凭据。

```powershell
.\scripts\devflow.ps1 start -Demo
.\scripts\devflow.ps1 status -Demo
.\scripts\devflow.ps1 stop -Demo
```

```bash
bash scripts/devflow.sh start --demo
bash scripts/devflow.sh status --demo
bash scripts/devflow.sh stop --demo
```

脚本使用独立的项目（`devflow` 和 `devflow-demo`），因此演示状态不会与真实状态混合。两者都使用 3000 端口：
启动另一个之前请先停止当前的。没有导入项目时运行的演示不能导出已批准的导入项目补丁。

直接使用 Compose 时，将 `DEVFLOW_PROJECT_PATH` 设置为绝对仓库路径（或将 `.env.example` 复制为 `.env` 并编辑路径），然后运行：

```bash
docker compose -p devflow -f compose.yaml config --quiet
docker compose -p devflow -f compose.yaml up --build --detach --wait
```

无需 `.env` 或项目路径即可在任一 shell 中运行明确的演示：

```bash
docker compose -p devflow-demo -f compose.yaml -f compose.demo.yaml up --build --detach --wait
```

## 状态、日志、停止与离线备份

```powershell
.\scripts\devflow.ps1 status
.\scripts\devflow.ps1 logs
.\scripts\devflow.ps1 stop
.\scripts\devflow.ps1 backup -OutputDirectory 'C:\backups\devflow'
```

```bash
bash scripts/devflow.sh status
bash scripts/devflow.sh logs
bash scripts/devflow.sh stop
bash scripts/devflow.sh backup --output /home/me/backups/devflow
```

演示堆栈请追加 `-Demo` / `--demo`。按 Ctrl+C 退出日志跟随。停止会保留容器和卷，绝不会删除用户数据。
备份也会停止堆栈，并保持停止状态。它使用已构建的后端镜像，在一次离线操作中归档整个数据库/工作区卷，
不会拉取镜像或联系提供商。**凭据会被排除**；恢复后请重新输入凭据。备份仍包含源代码、提示词和运行记录，
必须受到保护。备份期间请停止所有其他写入者。参见[备份与恢复](docs/BETA.md#offline-backup-and-restore)。

## 运行时边界

- 只有前端发布 loopback 端口。后端健康检查保持在内部。
- 后端从配置的只读主机绑定读取 `/project`。它将 SQLite、导入内容和受管理的工作区存储在 `devflow-data` 中的 `/var/lib/devflow`。
- 提供商设置位于专用 `devflow-secrets` 卷的 `/var/lib/devflow-secrets`。该卷不会挂载到 dispatcher 或 candidate 容器。
- 只有受信任的 dispatcher 挂载 Docker socket。它从 `/var/lib/devflow/workspaces` 读取受管理的工作区并运行受约束的 candidate 检查。
  runner 镜像保留 DevFlow 的测试环境工具；项目依赖使用隔离的、仅 wheel 的环境，不运行任意包构建脚本。
- 前端代理同源 API/SSE 流量。已启用本地 origin 安全；这不是公开的多用户托管设置。不要发布后端 8000 端口，
  也不要为了绕过设置问题而禁用 origin/安全检查。
- 支持一个 coordinator 进程和一个 SQLite 数据库。Docker 隔离属于纵深防御，不能保证抵御恶意代码或内核逃逸。

## 开发与证据

贡献者命令请参见 [CONTRIBUTING.md](CONTRIBUTING.md)，安全报告请参见 [SECURITY.md](SECURITY.md)。历史作品集材料仅作背景，
不是 beta 验证证据。普通测试可以从源码运行：

```bash
cd backend
uv sync --locked --python 3.11
uv run --locked pytest -q
```

在另一个终端中，从源码根目录运行：

```bash
cd client
npm ci
npm run lint
npm test
npm run build
npm run test:e2e
```

通过 mock/unit 测试不能证明实时提供商兼容性、端到端 Docker 行为、Windows/Linux 启动或备份恢复。
已记录的 beta 修订版本和结果见[验证与发布记录](docs/GITHUB_RELEASE.md)。

## 许可证

[MIT](LICENSE)。现有许可证和版权声明保持不变。
