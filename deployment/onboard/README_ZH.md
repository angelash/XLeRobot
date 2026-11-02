# 车载小九独立运行环境

目标：笔记本自行维持智能会话和机器人服务，工作端通过消息与它协作；工程源码持续同步。导航实施方案见 [NAVIGATION_PLAN_ZH.md](NAVIGATION_PLAN_ZH.md)。

## 目录与职责

| 目录/进程 | 职责 |
|---|---|
| 笔记本 `D:\Robot\Workspace\XLeRobot` | 完整Git源码工作区，`origin`为个人维护仓库，`upstream`为原项目 |
| 笔记本 `D:\Robot\XLeRobot` | 已验收的控制服务运行目录，保留本机标定、日志和三路相机配置 |
| 笔记本 `D:\Robot\XLeRobot\.venv-debug` | 已安装的Python 3.11环境；后台会话桥仅用标准库 |
| 笔记本 `D:\Robot\Onboard` | 本机配置、持久会话ID、消息回执和诊断，不进入Git |
| `XLeRobot-Boot` | 原有控制服务和Studio开机任务，继续为唯一串口所有者 |
| `XLeRobot-Onboard` | 新增开机任务，运行本地会话桥和源码同步；不接管串口 |
| `127.0.0.1:8781` | 本地消息接口，仅loopback监听，工作端经已有严格SSH访问 |

源码同步与运行版本部署分开。源码更新不会覆盖运行目录或触发机器人动作；控制服务升级继续使用已有的空闲检查、回归、逐文件哈希与回滚流程。

## 智能调用方式

使用笔记本安装的小九随附 `codex.exe` 引擎，复用该机既有 `CODEX_HOME` 和账号。程序按 `exec`/`exec resume` 调用同一个专用会话，并保存会话ID；模型沿用本机配置，不复制工作端认证材料、不改变小九其他会话。

这是小九随附引擎的后台会话接入，尚未验收桌面窗口中的会话列表展示或桌面UI控制。后台运行不依赖桌面窗口常驻。

当前明确为**观察和规划模式**：`motion_enabled=false`，原生引擎使用`read-only`沙箱，指令明确禁止机器人写接口。没有开放导航或自动驱动。接入动作执行器前必须完成导航方案中的独立动作租约、停车看门狗与崩溃接管验收。

## 通信

在笔记本工作区运行：

```powershell
& D:\Robot\XLeRobot\.venv-debug\Scripts\python.exe -m deployment.onboard.client health
& D:\Robot\XLeRobot\.venv-debug\Scripts\python.exe -m deployment.onboard.client send --message-file D:\Robot\Onboard\message.txt --id operator-001
& D:\Robot\XLeRobot\.venv-debug\Scripts\python.exe -m deployment.onboard.client read --id operator-001
```

工作端把UTF-8消息文件传到笔记本，再经SSH运行相同命令。提交立即返回；模型在笔记本后台执行，不要求SSH持续连接。读回回执包含车载智能的回复和会话ID，可以继续发送下一条消息。两端无需共享串口，也不通过IM向第三方发送消息。

接口：

| 请求 | 含义 |
|---|---|
| `GET /health` | 服务、会话、队列、故障、源码同步状态 |
| `GET /v1/presentation` | 最近32条消息的公开展示投影，不含私有诊断、原始引擎事件和会话密钥；供[形象与过程展示](../../presentation/README_ZH.md)读取 |
| `POST /v1/messages` | `{"id":"unique-id","sender":"workstation","message":"..."}` |
| `GET /v1/messages/{id}` | 持久回执；状态为queued/running/completed/failed/cancelled/interrupted |
| `POST /v1/cancel` | 取消当前模型进程树，并阻止队列继续运行 |
| `POST /v1/reset` | 人工确认故障后清除锁定；运行中拒绝；不恢复或重放旧消息 |

POST需要`application/json`；拒绝外部Origin。请求ID只能包含字母、数字、下划线和短横线。相同ID与相同内容重复提交只返回原回执；同ID不同内容拒绝。

机器人展示使用`sender=presence`，另外传入`presentation={"name":"MOSS","persona":"冷静、简洁、准确。"}`。
当前角色由每条消息的配置快照决定，随本轮角色台词生成任务显式传入；更新角色设置后下一条消息生效。
展示对话保存于独立`presentation-session.json`，与后台工程会话`session.json`分离；两者各自延续，旧工程会话和历史记录保留。
角色仅影响展示身份与表达，观察/规划边界与只读沙箱继续生效。后台普通工作端消息仍使用车载小九身份；公开回执只投影用户原文与公开回复，不包含角色配置或私有引擎事件。
`health.presentation_context_version=2`表示运行进程支持独立展示会话及逐轮角色上下文；源码同步后须在空闲时恢复既有任务并核对该字段，不能只确认磁盘源码已更新。

## 超时、崩溃和恢复

- 客户端HTTP超时为5秒，消息提交与模型执行分离；请求可能已入队时，用原ID查回执，不换ID盲目重发。
- 模型轮次默认120秒超时。超时或取消后结束本轮拥有的进程树；不把部分回复、非零退出或缺失完成事件当成功。
- 一轮失败锁定后续队列，排队消息取消；需检查后显式reset。
- 服务重启把queued/running消息记为interrupted，锁定新请求，不自动重放。
- 端口先绑定成功才启动工作线程，防止两个服务实例执行同一队列。
- 开机脚本将父进程PID交给服务；父进程退出时，服务结束自己拥有的引擎并关闭监听，避免Windows停止任务后留下孤儿Python进程占端口。
- 此桥当前不持有任何动作，因此上述异常不会留下它发出的运动指令。底盘和控制服务崩溃后的停车仍是导航实施阶段的强制门槛，不能用会话取消测试代替电机停车实测。

## 持续源码同步

后台默认每60秒检查`origin/main`，只在笔记本工作区干净且可快进时更新。处理本机编辑、网络错误和分叉：

- 有本地改动或未跟踪文件：`blocked_local_changes`，保留文件，不覆盖、不暂存、不自动提交。
- 本地分支与远端分叉：`blocked_divergence`，不reset、不自动rebase。
- 网络/命令超时：`unavailable`，记录后下一轮再检查，不影响已运行的控制服务。
- 两端工程修改以Git提交为同步单位；尚未提交的编辑不自动传播。笔记本端修改可通过独立分支或patch回传，合入维护仓库后两端收敛。

一次手动检查：

```powershell
& D:\Robot\XLeRobot\.venv-debug\Scripts\python.exe -m deployment.onboard.sync_source --git D:\RobotTools\MinGit-2.53.0.2\cmd\git.exe --workspace D:\Robot\Workspace\XLeRobot
```

源码变化会显示最新Git提交；已经运行的Python进程仍使用启动时加载的代码。升级会话桥时应等队列空闲，再重启`XLeRobot-Onboard`。不要把源码已同步写成运行服务已升级。

## 配置和开机任务

机器私有配置示例（按实物路径填写，保存在Git外）：

```json
{
  "executable": "C:\\Program Files\\xiaojiu-ai\\resources\\runtime\\codex\\vendor\\x86_64-pc-windows-msvc\\bin\\codex.exe",
  "code_home": "本机既有小九profile下的codex-home绝对路径",
  "workspace": "D:\\Robot\\Workspace\\XLeRobot",
  "state_dir": "D:\\Robot\\Onboard\\state",
  "git": "D:\\RobotTools\\MinGit-2.53.0.2\\cmd\\git.exe",
  "port": 8781,
  "turn_timeout": 120,
  "sync_interval": 60
}
```

在已授权的管理员会话中：

```powershell
& .\deployment\onboard\register-onboard.ps1 -Workspace D:\Robot\Workspace\XLeRobot -PythonExe D:\Robot\XLeRobot\.venv-debug\Scripts\python.exe -Config D:\Robot\Onboard\config.json -Apply
Start-ScheduledTask -TaskName XLeRobot-Onboard
```

任务采用S4U，不保存用户密码；开机延迟30秒，失败每分钟重试，允许电池运行。移除新桥时只停用/删除`XLeRobot-Onboard`，保留原有`XLeRobot-Boot`与工程资料。会话记录可保留以便恢复。

## 验证

```powershell
.\.venv-debug\Scripts\python.exe -m pytest deployment/onboard/test_onboard.py -q
```

覆盖消息去重、崩溃后不重放、失败锁定队列、模型超时结束进程、部分回答拒绝、HTTP通信、取消启动竞态、父进程退出清理及源码更新保护。车载运行环境的pytest由原部署测试工具目录提供，远程回归时先设置`PYTHONPATH=D:\Robot\XLeRobot\.local-deploy\test-tools`，并将便携Git加入该次命令的PATH；不要求把测试依赖安装进控制环境。实际车载部署、跨进程会话连续性、完整文件校验和开机任务运行结果见`PROJECT_STATE.md`最新记录。

## 语音输入与常驻对话（2026-11-02）

默认使用`AppServerBrain`，由本进程托管私有stdio `app-server`子进程，无新增端口或任务。首次请求懒加载，后续轮次复用进程；仍分别续接`session.json`与`presentation-session.json`。只把allowlist中的`agentMessage`公开回复送入同源SSE；analysis、未知phase及工具原文不转发。成功必须收到completed终态，取消/超时/崩溃不自动重试或换适配器重发。

新增`POST /v1/messages/{id}/cancel`只取消指定展示请求；原`/v1/cancel`仍是全桥故障锁定。公开增量通过`GET /v1/presentation/stream`进入8770。需要维护回退时，可在私有配置中明确设`"engine_transport":"cli"`并在空闲时重启；它不是运行时自动回退。

本机麦克风入口为`http://127.0.0.1:8770/presence/`，输入默认关闭且需要用户手动授权、选择设备。准备步骤、静默验证与用户验收见[语音输入交付说明](../../docs/presence/VOICE_INPUT_DELIVERY_ZH.md)。
