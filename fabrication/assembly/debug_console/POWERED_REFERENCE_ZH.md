# 自检后补齐整机联动参考

限位自检回答关节能走到哪里；末端IK还需要肩抬升、肘和腕俯仰的几何参考及夹爪开合方向。只有行程的旧标定不能自动当作完整运动学标定。

本次流程通过8765唯一串口所有者低速摆到A/B/C和夹爪开合位置，由助手查看头部及另一只手腕的侧面画面复核，再补充软件参考。没有关闭双臂扭矩、改舵机零点、重设行程或开启手柄接管。入口继续使用8770 Studio。

`reference_calibration.py`在车载仓库运行。输入清单绑定物理控制板，逐卡引用成功助手运行的`snapshot`步骤及几何复核说明。A/B/C含义与[自由采集与整机标定](WHOLE_CALIBRATION_ZH.md)一致。图片、运行记录、输入清单和实际标定保留在私有证据目录，不提交仓库。

```powershell
.venv-debug/Scripts/python.exe -X utf8 -m fabrication.assembly.debug_console.reference_calibration <复核清单.json>
# 先生成proposed-profile.json，核对后应用：
.venv-debug/Scripts/python.exe -X utf8 -m fabrication.assembly.debug_console.reference_calibration <复核清单.json> --apply
```

清单需要`profile_sha256`、`arm_ports`、`runs_root`与`cards`。卡片名为`left_a/b/c/open/closed`及`right_a/b/c/open/closed`；每卡包含`port`、`run_id`、`step`、`geometry_reviewed: true`及实际观察的`review_note`。这些字段记录已经完成的观察，不能用勾选替代真实画面复核。

工具检查现有行程、限位与homing、A/B/C变化、夹爪端点及原始图像存在，拒绝失败运行、映射改变、并发标定修改和进行中的接管/自检/采集。应用前按原字节备份，通过既有`/api/calibration/read`让当前串口所有者重载；随后核对整机就绪状态和16电机配置，异常时恢复本次原文件。工具只读设备状态，不写舵机寄存器，不调用启动接管接口。

相机`/api/camera/{0|1|2}/snapshot`返回现有MJPEG采集者的最近画面，带`X-Captured-At`与`X-Frame-Sequence`，超过2秒或采集者已退出则拒绝。这个时间是服务取得JPEG的时间，不是相机曝光时间。同一路的第二个MJPEG采集者会被拒绝，避免重复打开UVC。没有实时流时，仍可用既有助手`snapshot`抓图；不要另开ffmpeg争用正在播放的设备。

这是画面几何与编码器共同确定的低速遥控初始化，不是测量级末端定位、碰撞规划或全工作空间验收。小步联动核验记录三关节实际响应；模型中的毫米值不等于外部测量。既有助手`move`普通模式允许静止时小于72计数的跟随差，`done`不证明精确到达；需要精确判断时独立核对回读，不把收起回位误当定位精度证明。

2026-11-02实机记录：左右各5张参考已保存，整机检查的双臂、底盘接口和完整行程均为就绪；每个关节参考绑定原物理板和原坐标。末端小步、收起和最终状态另在项目台账记录。手柄输入、急停、互斥、温度/电压和碰阻规则继续按[助手自主控制规范](AGENT_CONTROL_ZH.md)执行。
