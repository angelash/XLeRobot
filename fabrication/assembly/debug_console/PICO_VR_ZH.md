# PICO Neo3 VR 遥控

入口：原 XLeRobot Studio 的 **VR遥控**，PICO 上打开 `https://192.168.137.150:8770/vr/`。与键盘遥控共用同一个8770应用、同一个网页控制会话和8765唯一串口所有者。需求分析与取舍见 [PICO_VR_REQUIREMENTS_ZH.md](PICO_VR_REQUIREMENTS_ZH.md)。

> 状态（2026-11-26）：页面、服务端映射、同端口 HTTPS 与安装包已在工作端完成并通过离线测试；**尚未上车部署，PICO 真机按键、证书行为和实机跟随效果都没有验证。** 首次上车按第 6 节的顺序逐步验收。

## 1 手柄映射

| 输入 | 功能 |
|---|---|
| 左摇杆 | 前后：前进／后退；左右：左转／右转。松开立即停车 |
| 左摇杆按下 | 底盘快／慢档（慢档 50%） |
| 右摇杆 | 左右：头部转向；前推抬头，后拉低头 |
| 右摇杆按下 | 手臂精细模式（位移比例减半） |
| 左／右 握把（按住） | 同侧机械臂跟随手柄；按下瞬间自动对齐，松开手臂停住 |
| 左／右 扳机 | 按深浅控制同侧夹爪；夹到物体保持轻夹，松开扳机张开 |
| X 或 A 短按 | 重新校准：以当前面对方向为正前方，并把画面摆到眼前（握把按住时无效） |
| Y | 只把画面摆到眼前 |
| X + A 同时按住 1.5 秒 | 开启实机控制 |
| B | 急停：结束实机控制，关节保持，底盘停车 |

物理左臂对应左手，左腕相机画面在左下方；左右绑定沿用控制设置里的手臂映射。

## 2 手臂怎样跟随

- **离合式对齐**：按住握把的瞬间，服务端读取该臂当前编码器，算出夹爪在二连杆平面中的位置和夹爪朝向；页面记下手柄当前位置。之后只把“手相对按下时的移动量”映射到夹爪，所以任何时候按下都不会跳。
- **重置**：松开握把→把手挪到舒服的位置→再按住，就重新对齐。
- **方向**：以校准时头显面对的方向为身体正前方。手前移→夹爪沿手臂当前朝向前伸；手左右移→肩水平转动；手上下→夹爪升降。默认 1:1，可在页面改 0.25–2 倍。
- **姿态**：手柄抬头／低头改变夹爪俯仰（绕夹爪中心转），手腕左右拧改变腕旋转。可在页面关掉“夹爪俯仰／旋转跟随手腕”，只跟位置。
- **边界**：手超出手臂可达范围时，手臂停在最远处并在状态面板显示“到边界”；碰到关节限位时只走到限位；手退回即继续跟随。手柄位移超过 0.6 米视为追踪跳变，本帧丢弃。
- **速度**：每个关节按“控制设置里的逐轴速度 × 页面速度比例”逐帧逼近目标，VR 另设 45°/s 上限，不会超过逐轴设置。默认逐轴 12°/s，手快时手臂会慢慢追上；需要更跟手时，先在控制设置里小幅调高并在空旷处试。
- **前提**：手臂跟随需要整机标定保存的 A/B/C 参考姿态（肩抬升、肘、腕俯仰）和夹爪开合端点；缺少时该臂显示“不可用”及原因，其他通道照常。折叠到模型外的姿态（例如完全收起）拒绝接合，先用键盘或手柄把手臂伸开一些。

## 3 安全与停止

- 输入约 20 次/秒随 VR 画面帧发送；650 毫秒没有新输入即触发停止。摘下头显、头显追踪丢失、坐标系重置、按系统键、VR 画面被系统界面遮挡、退出 VR、页面切到后台或断网，都会停止，且不会自动恢复旧输入；需要松开 X/A 后重新同时按住 1.5 秒开启。实际总线与物理停车延迟待上车测量。
- 开启请求尚未返回时，B、退出或失焦同样取消本次开启；迟到响应返回的会话会立即收到停止请求。单侧手柄丢失时，不把未知扳机值当作松开，夹爪保持；恢复追踪后先重新按扳机再接管夹爪。
- 实体 Xbox 手柄任意输入结束 VR 会话；手柄接管、助手运行、自检、摆位、抓取任务和 Studio 录制都与 VR 控制互斥。
- 继承网页遥控的全部关节保护：设备限位、温度／电压／模式／扭矩异常停止，编码器受阻停止。夹爪受阻是例外：保持最多 40 计数的轻夹，不结束会话；扳机松到接触点外侧后恢复跟随。
- 底盘与键盘页一致：最大 160 raw，约 0.8 秒窗口检查两轮是否按命令方向和比例转动；这不是碰撞检测，净空由人判断。
- 不修改 ID、模式、PID、零点或固件，不扩大限位。

## 4 安装（需要用户授权的车载维护窗口）

安装包由工作端生成：`dist/xlerobot-pico-vr-<日期>.zip`（含车载私钥，**不要外传或上传**）。生成命令：

```powershell
.\.venv-debug\Scripts\python.exe deployment\pico_vr\build_apk.py --sdk D:\androidSdk --jdk "C:\Program Files\Java\jdk-17"
```

```powershell
.\.venv-debug\Scripts\python.exe deployment\pico_vr\build_package.py --base 62e9111
```

`--base` 是车载笔记本当前运行的提交；安装程序只替换与该版本一致的文件，车上文件被改过就拒绝。

### 4.1 车载笔记本

1. 把 zip 复制到笔记本并解压，例如 `D:\Robot\pico-vr\`。
2. 先只看计划（不改任何东西）：

```powershell
powershell -ExecutionPolicy Bypass -File D:\Robot\pico-vr\xlerobot-pico-vr-20261126\vehicle\install-pico-vr.ps1
```

   计划会列出每个文件是新增、更新、已是新版还是冲突，显示根证书 SHA-256 指纹，以及当前是否有接管／录制挡住重启。
3. 确认空闲后安装。VR 输入接口在 8765 控制服务里，需要重启控制服务才生效：

```powershell
powershell -ExecutionPolicy Bypass -File D:\Robot\pico-vr\xlerobot-pico-vr-20261126\vehicle\install-pico-vr.ps1 -Apply -RestartController
```

   不加 `-RestartController` 时只重启 Studio，VR 页能打开但实机控制要等控制服务下次重启（例如下次开机）。安装步骤：备份→复制→安装 HTTPS 证书→在 `studio-network.json` 加 `tls` 段与 https 来源→跑车载回归→重启→用本地根证书验证 `https://127.0.0.1:8770/vr/`。任何一步失败都恢复原文件和原配置并重启回旧代码。备份在 `.local-deploy\pico-vr-install-<时间>\`。
4. 放行 PICO：PICO 连上热点后在“设置→WLAN”里查看它的地址（例如 `192.168.137.23`），用管理员 PowerShell 运行带 `-PicoAddress 192.168.137.23` 的安装命令，或单独运行 `deployment\windows\configure-studio-network.ps1`，把 PICO 地址加到 `-ClientAddress` 列表（保留工作端 `192.168.137.1`）。热点给 PICO 换了地址时重新运行。

### 4.2 PICO Neo3

1. 打开开发者模式，用 USB 连电脑：

```powershell
adb install -r D:\Robot\pico-vr\xlerobot-pico-vr-20261126\pico\XLeRobotVR.apk
```

   安装后在 PICO 资源库的“未知来源”里找到 **XLeRobot VR**。
2. 打开 XLeRobot VR，填机器人地址（默认 `192.168.137.150`）：
   - ① 检测连接：显示 Studio、控制服务和 HTTPS 是否就绪；
   - ② 安装机器人证书：显示证书 SHA-256，与车载安装程序输出的指纹核对一致后确认安装；
   - ③ 打开 VR 遥控：用 PICO 浏览器打开 VR 页。浏览器不支持 WebXR 时，可在 PICO 商店装 Wolvic，用“用其他浏览器打开”。
3. 不装 APK 也可以：在 PICO 浏览器直接输入 `https://192.168.137.150:8770/vr/`；证书提示可选“继续访问”，或先打开 `http://192.168.137.150:8770/vr/ca.crt` 下载证书到设置里安装。

如果 PICO 浏览器在证书例外下仍不开放 VR，备选：PICO 用 USB 连车载笔记本，`adb reverse tcp:8770 tcp:8770` 后打开 `https://localhost:8770/vr/`（localhost 证书已包含）。

## 5 使用步骤

1. 在 VR 页选择 **预演**，进入 VR：画面、状态面板和右侧示意图出现。核对：握把后手前移，示意图夹爪前移；扳机让示意夹爪变小；左摇杆前推底盘读数向前。状态面板会显示按下的按键编号——若与第 1 节不一致，退出 VR，在页面把“按键编号”换成“上游 XLeVR 编号”再试。
2. 退出 VR，选择 **实机**，确认机器人周围无人、勾选安全确认，再进入 VR。
3. 同时按住 X+A 1.5 秒开启实机控制（手柄震动一下），再用右摇杆小幅转头，看到画面跟着动。
4. 小幅试：按住一侧握把，手慢慢上下移动几厘米，看夹爪是否同向；不对立即松开握把或按 B。
5. 用完按 B 或退出 VR。

## 6 首次上车验收顺序（待做）

按[开机测试清单](../../../docs/autonomy/POWER_ON_TEST_PLAN_20261123_ZH.md)先核验16电机、停车和相机新帧，并保留 `uncommanded_pose_change` 锁与右肩 hold 的现状（VR 不清除任何锁存或 hold）。然后：

1. 安装计划（只读）→ 安装 → 车载回归 → 从工作端和 PICO 分别打开 `/vr/`。
2. 预演：记录 PICO 实际按键编号、手柄前后左右方向、头显画面布局与帧龄。
3. 实机：先只用右摇杆转头和扳机开合；再单臂小范围跟随（手移动 3–5 厘米），核对方向与比例；再双臂；最后底盘短距。
4. 抓放一次真实物体。
5. 记录实际跟手刷新率（状态面板“往返／服务端处理”）与延迟，决定是否需要调整逐轴速度或总线读写方式（另行评审）。

每步结果写入 PROJECT_STATE.md，新发现写入 `AGENT_LEARNING_ZH.md` 待审核条目。

## 7 接口

- `POST /api/web/start`：`{"confirmed":true,"mode":"vr"}`，返回会话编号。
- `POST /api/web/vr-input`：`{"session_id","seq","base":[前进,左转],"head":[右,上],"arms":{"left":{"engage":n,"pos":[前,左,上],"pitch":度,"roll":度,"orient":true}|null,"right":…},"grip":{"left":0–1,"right":0–1},"scale":0.2–1,"ratio":0.25–2}`。`pos` 是手相对按下握把时的位移（米），`engage` 每次按下握把加一，服务端据此重新对齐。
- `POST /api/web/stop`、`GET /api/web/status`（含 `mode` 与 `vr` 状态：各臂空闲／跟随中／到边界／不可用及原因、夹爪是否轻夹、服务端单拍处理毫秒）。
- `GET /vr/`、`GET /vr/ca.crt`（只提供配置的公开根证书）。

## 8 验证

```powershell
.\.venv-debug\Scripts\python.exe -m pytest fabrication/assembly/debug_console/test_vr_teleop.py fabrication/assembly/debug_console/test_studio_tls.py fabrication/assembly/debug_console/test_web_teleop.py fabrication/assembly/debug_console/test_platform.py -q
```

```powershell
node fabrication/assembly/debug_console/test_vr_mapping.cjs
node fabrication/assembly/debug_console/test_vr_client.cjs
```

安装程序的隔离验证（Windows PowerShell 5.1，HTTP、服务与任务操作均用假实现，不接硬件）：

```powershell
.\.venv-debug\Scripts\python.exe -m pytest deployment/pico_vr/test_install_package.py -q
```

该组覆盖只读计划、源码冲突、伺服／自检占用、录制状态未知、目录越界、中途复制失败、新旧文件回滚、回归失败不重启控制器、HTTPS验证失败恢复旧服务，以及安装期间新接管优先。安装前与实际重启前都会重新检查占用。

测试用假 STS 总线与本机回环套接字，不接触硬件。另在工作端浏览器用模拟头显（伪造 `navigator.xr`、手柄与控制接口）走通了：X+A 开启、握把离合、位移换算、摇杆、扳机、松开、B 急停，以及 VR 初始化失败时的提示。软件验证不替代 PICO 真机、实机方向、弱网或全身碰撞试验。

2026-11-26续接结果：隔离源码中上述服务端回归，加原整机遥控、空间记录和安装程序验证，共142项通过（2项既有依赖弃用警告）；另有7项真实页面脚本的模拟WebXR交互检查与Node映射检查通过。隔离源码使用`agent_ops.py`／`test_agent_ops.py`已提交版本，未依赖工作区两份既存修改。工作端内置浏览器检查了桌面与390像素窄屏的准备状态／表单；桌面缺沉浸设备时显示正确提示。APK已用Android SDK 34、JDK 17构建并通过签名验证。安装程序测试以假HTTP、假服务和假Python进程运行，不能视为车载安装或Windows防火墙实测。
