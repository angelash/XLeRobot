# 整机实机首次标定

在Studio的“控制”页打开原版手柄控制，点击“开始整机标定向导”。低速试控使用当前位置附近的编码器窗口；整机末端联动需要真实关节角度和完整可用行程，不能用静止读数代替。

## 现场步骤

1. 结束手柄接管，支撑双臂重量、留出线缆余量。勾选现场确认并开始；程序先持久备份16颗电机寄存器，再关闭双轮速度与全部扭矩，不发送位置目标。
2. 一次只动一个关节，缓慢走过完整可用范围，再回中。双臂各6轴和头部2轴分别勾选。不要强推机械止挡或拉紧线缆。页面显示实际跨度；采样中断、异常快转或坐标变化会中断本次标定，需要重新开始。
3. 按图完成左臂A/B/C，再记录左夹爪全开、闭合。右臂重复。各姿态稳定约1秒再采集。
4. 双臂放稳，确认周围有空间；分别短测底盘前进和左转。程序检查编码器位移、零速与扭矩关闭；人确认车体的真实方向。若方向不符，修改轮子方向并重测两项。
5. 双臂回B附近，夹爪半开，肩水平、腕旋转和头部在行程中部。保存后重新核对整机初始化。扭矩仍关闭；手柄回中、确认现场，点击“开启整机控制”。

只有第五步保存成功，正常入口才使用完整实测范围。页面刷新能继续当前运行进程内的采集；服务重启后保留私有证据，需重新开始采集，不自动恢复释放或接管。

## A/B/C姿态定义

看肩、肘、腕三个旋转轴中心之间的连线，不看塑料外壳边缘。朝外指该机械臂离开车体的方向；两臂分别摆放，不需同时举起。

- **A**：肩→肘竖直向上，肘→腕水平朝外，夹爪朝向水平。
- **B**：肩→肘朝外倾45°，前臂和夹爪仍水平，腕相对前臂不变。
- **C**：保留B的肩、肘位置，只转腕俯仰，让夹爪尖端抬起约45°。

模型定义来自`upstream_teleop.py`：A对应`lift=-deg(O1)`、`elbow=deg(O2)`；A→B的模型变化是肩+45°、肘−45°，真实编码器变化决定正负方向。腕以B为局部0，C对应−45°。这套人工对位用于遥控初始化，不能声称为高精度测量标定。

## 底盘资料值

本地0.4装配文档`docs/en/source/hardware/getting_started/assemble_2wheel.md`指定5英寸轮，即直径127mm。双轮源码`software/src/robots/xlerobot_2wheels/config_xlerobot_2wheels.py`默认轮距250mm，但该文件的默认轮径是100mm，两处资料并不一致。

当前按用户“按资料来”的选择，采用装配页轮径127mm和源码轮距250mm，保存`geometry_source=published_design`及两个出处。它们是资料参数，不标成实测；后续实测尺寸可在标定文件中更新并重新验方向/停车。遥控仍低速，不能把显示里程当准确测距。

## 写入与异常处理

- 使用同一个控制服务的串口对象和I/O锁；标定期间禁止准备实机、开启扭矩和启动任何手柄模式。
- 扭矩关闭时持续读取12位编码器，用相邻样本展开跨零；超过2秒的采样间隙或过快移动不猜跨零。
- 以观察到的范围居中重设sign-magnitude homing，端点内缩8计数；不改ID、模式、PID。每颗配置写入先将输出上限48设0并回读。
- 已实测固件3.10写42会自动开启扭矩。因此位置目标只在输出上限0时写，随后独立写40=0并回读，再恢复输出上限。不会把“写当前位置”当成无动力操作。
- 批量写入失败时回滚所有已触及电机的范围、homing和增益。恢复无法确认的电机保留零输出；页面显示错误，不能继续接管。
- 原始寄存器、参考姿态、范围证据和回滚资料留在`fabrication/assembly/evidence/<日期>/whole-calibration/`。实际标定文件为`fabrication/assembly/kinematic-calibration.json`。均不提交Git。

底盘测试的180ms是普通系统定时器目标，受调度和串口超时影响，不提供硬实时停车保证。现有双轮/输入/网页看门狗仍生效。标定与遥控不包含碰撞规划。

## 验证

```powershell
.venv-debug/Scripts/python.exe -X utf8 -m pytest fabrication/assembly/debug_console/test_control_runtime.py fabrication/assembly/debug_console/test_original_teleop.py fabrication/assembly/debug_console/test_platform.py fabrication/assembly/debug_console/test_commissioning.py fabrication/assembly/debug_console/test_whole_calibration.py -q
node fabrication/assembly/debug_console/test_original_dashboard.cjs
node fabrication/assembly/debug_console/test_whole_calibration_dashboard.cjs
```

自动测试使用假STS总线，覆盖跨零、方向、动作互斥、持久备份、ACK丢失后的全批回滚、完整接管、夹爪物理速度和网页步骤。通过测试不等于已完成现场标定或整机物理验收。
