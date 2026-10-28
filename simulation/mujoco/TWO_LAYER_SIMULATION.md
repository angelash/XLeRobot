# XLeRobot 两层低位模式仿真

## 目的

验证一个具体假设：

> 不安装宜家 RÅSKOG 的最上层托盘，把双臂基座降低约一层，是否能够收拾地面玩具？

该实验是对现有 MuJoCo 模型的运动学可达性补充。它不是精确的拉斯克碰撞模型。

## 配置

- 上游三层配置的双臂根部高度：0.7865 m。
- 两层示意配置的双臂根部高度：0.5465 m。
- 模拟下移量：0.24 m。
- 头部也同步下移 0.24 m。
- 低位场景增加两块示意托盘，省略顶层。
- 地面玩具中心：x=0.50 m、y=-0.10 m、z=0.045 m。
- 夹爪末端使用 Fixed_Jaw 与 Moving_Jaw 两个刚体原点的中点近似。

## 运行

从仓库根目录执行：

```powershell
.\.venv-sim\Scripts\python.exe .\simulation\mujoco\two_layer_demo.py
.\.venv-sim\Scripts\python.exe .\simulation\mujoco\two_layer_reach_scan.py
```

## 当前结果

- 原顶部配置的最低夹爪中点高度：0.688 m。
- 两层低位配置的最低夹爪中点高度：0.437 m。
- 地面玩具中心高度：0.045 m。
- 两层配置到地面玩具的最近优化距离：0.455 m。

因此，按当前几何和关节范围，只省略第三层并把机械臂降到中层，仍不能捡起地面物品。它明显扩大了向下的工作范围，但没有到达地面。

## 结果文件

- artifacts/two_layer/comparison.png
- artifacts/two_layer/normal_reach.png
- artifacts/two_layer/two_layer_reach.png
- artifacts/two_layer/min_z_pose.png
- artifacts/two_layer/report.json
- artifacts/two_layer/min_z.json

## 尚未验证

- 机械臂或夹爪与托盘、立柱、电源和轮组的实际碰撞。
- 两层结构的重心和行驶稳定性。
- 头部相机在低位时的有效视野。
- 5264 线、USB 线和电源线的长度与安全走向。
- 将机械臂翻转、前伸或安装到更低托盘之后的真实可达范围。
- 相机标定、逆运动学参数和采集策略的迁移。

## 结论

两层结构值得作为可拆装的“低位模式”实验，但不能仅凭减少一层就承诺能够拾取地面物品。若目标明确是地面玩具，应进一步模拟并制造：

1. 可向外展开的低位机械臂连接架。
2. 向下的机械臂安装姿态。
3. 更长、可更换的夹爪或扫取附件。
4. 升降机构。

所有方案都必须先经过托盘与轮组干涉检查，再进行真机低速测试。
