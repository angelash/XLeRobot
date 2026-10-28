---
title: XLeRobot 可制造化与硬件工坊路线
last_updated: 2026-10-21
status: concept-and-first-article
---

# 可制造化目录

该目录保存 XLeRobot 机器人后续改件的 CAD 源码、导出物、打印参数和首件验证证据。

## 第一件改件：低位前伸接口

目标来自前面的 MuJoCo 对比：现有三层高位不能拾取地面物品；两层方案仍差约 0.28 m；当前更适合制作一个**可拆的低位前伸架**，而不是把整条机械臂直接固定在推车最底层。

第一件可打印零件是 `xlerobot_040_arm_interface.stl`：

- 外轮廓按 `hardware/step/XLeRobot_040/XLeRobot040_armbase.step` 中的中央双臂安装接口测得为约 150.5 × 140 mm；
- 六个 M3 过孔位来自 STEP 的圆边中心；
- 另加四个 M5 孔，用于连接 6 mm 铝背板和 2020/2040 铝型材框架；
- 厚度 8 mm，仅作为接口件，不把打印塑料当作全长悬臂梁。

生成命令：

```powershell
python -m venv .venv-cad
.\.venv-cad\Scripts\python.exe -m pip install -r fabrication\requirements-cad.txt
.\.venv-cad\Scripts\python.exe fabrication\cad\xlerobot_low_front_prototype.py
```

导出物位于 `fabrication/export/`。概念装配按向前 120 mm、向下 207 mm 建模；这两个数字来自简化仿真目标，不能代替实物尺寸和承载测试。

## 打印与首件规则

1. 先用 PLA 打一块低成本接口量具，干装检查六个 M3 孔、孔边余量和原基座是否干涉。
2. 量具通过后，正式结构件优先使用 PETG-CF、PA6-CF 或 ASA。不要用普通 PLA 承担长期悬臂和振动载荷。
3. 承受机械臂冲击的载荷主要通过钢制/铝制背板、2020/2040 铝型材和 M5 螺栓传递。打印件负责定位、夹紧和隔离，不应单独承载整条机械臂。
4. 首次打印建议 0.4 mm 硬化喷嘴、0.20 mm 层高、6 道墙、上下各 5 层、40%–50% gyroid/3D honeycomb、无支撑打印方向。切片结果必须在 Bambu Studio 中复核。
5. 打印后做孔位、平面度、扭矩、温升、振动、急停和 2 小时重复动作测试。未通过前不得挂载双臂持续运行。

## 结构边界

- 当前 CAD 是“首件概念”，不是安全认证结构。
- 原机器人约 12 kg，机械臂移动会产生明显惯性；底盘移动前应收回机械臂或使用锁定姿态。
- 低位架会改变相机外参、线束路径和重心，需要重新标定。
- 打印件失效可能造成夹爪或机器人跌落，因此正式版本需要金属主结构和失效保护。

## 文件

- `cad/xlerobot_low_front_prototype.py`：参数化生成脚本。
- `export/xlerobot_040_arm_interface.step`：可编辑首件接口。
- `export/xlerobot_040_arm_interface.stl`：可导入 Bambu Studio。
- `export/xlerobot_040_arm_interface.svg`：孔位和轮廓审阅图。
- `export/xlerobot_040_low_front_concept.step`：低位架概念装配，含铝型材与接口件。
- `export/xlerobot_040_low_front_concept_preview.png`：审阅预览。
- `export/xlerobot_040_low_front_metadata.json`：尺寸、孔位和状态。
