# 接线前软件准备状态

> 2026-10-28 更新：以下为历史准备记录。当前本机使用 [Web 控制台 v2](debug_console/README.md)，不再使用旧独立首次动作进程。v2 保留设备限位，真实编码器小幅往返已验证；旧的±512“初始校准”不作为放行依据。

日期：2026-10-27。用户已断开硬件，要求先完成软件准备。

## 已完成

- 建立独立硬件环境 `.venv-robot/`，安装 LeRobot 0.4.4、Feetech SDK、pygame 和 pytest。未修改系统 Python、仿真和 CAD 环境。
- 新增 `first_motion_controller.py`：首次试控直接使用 Feetech 总线，不启动整机旧配置；扭矩初始 OFF，LB 为 dead-man，方向帽一次按下只走一步，单次步长硬限制为最多 80 raw ticks，车轮保持离地，Ctrl+C/异常/松 LB 会停止并断开。
- 新增 `calibrate_first_motion.py`：扭矩全程 OFF，人工托住手臂，先居中，再逐关节走过真实可用行程；保存到 `evidence/2026-10-27/first-motion-calibration.json`，同时写入电机。
- 新增 `launch.ps1 -Mode status|calibrate|move`：只读枚举、校准和首次受限动作分开，防止一键直接开放全动作。
- 新增 `test_first_motion_pure.py`，无硬件单元测试通过 2 项。
- 首次动作脚本通过 CH343 USB 序列号识别左右控制板，不依赖重新枚举后变化的 COM 编号；两块不在时拒绝启动。

## 首次接线顺序

1. 硬件全部断电。两块控制板优先直接插电脑不同 USB 根口，或使用有独立供电的 Hub；不要带电插拔。
2. 运行 `fabrication/assembly/launch.ps1 -Mode status`。应显示 COM3/COM4 的两个 CH343，以及 Xbox One Elite 2 Controller。
3. 确认手臂被托住、脚轮固定、驱动轮离地、适配器朝外无拉扯。
4. 运行 `launch.ps1 -Mode calibrate`。先 COM3 再 COM4；按提示居中，然后逐关节缓慢走过真实安全行程。全程扭矩 OFF。
5. 运行 `launch.ps1 -Mode move`。先在控制台确认，再按住手柄 LB 才有扭矩；十字键左右选关节，上下每次走一步；松 LB 立即停。
6. 只试验一个关节的 1–2 次小步，确认方向、是否卡住。若方向反、噪声大、线缆拉紧或出现掉线，立即松开 LB 并 Ctrl+C。

若 COM 号码变化，脚本仍按固定序列号映射；状态页显示的 COM 只是当前电脑分配值。

## 当前边界

- 代码尚未连接真实硬件测试，不能宣称为已验证控制。
- 轮子和夹爪暂不在此脚本内开放；先用手验证臂与头部。
- 现有 XLeRobot 双轮整机插件面向的是旧版 LeRobot API，Windows 插件入口也只是文本，不强行使用；首个试控采用上述隔离脚本，通过后再兼容整机插件与网页界面。
- 真实动作前需确认适配器标签与极性、两个 CH343 的 COM 对应、物理左右和关节方向。网页仍为只读观测。
