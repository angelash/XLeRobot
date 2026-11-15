# P1接口、数据契约与离线使用

所有HTTP路径都属于既有Studio 8770。此阶段输入为**回放数据**，不认证当前传感器新鲜度、不执行轮控、不接触相机/串口。真实姿态、时间、尺度与标定由上游适配器负责，示例中的标定名称是示例，不代表实物已经标定。

## 1 在原Studio检查

原导航“空间观察” `/#fieldmap` 中已有“本地建图”面板；同一应用内的专用检查视图为`/fieldmap/metric/view`。点击“生成合成环境回放”创建新的非家庭场景，再点击“寻找下一观察位”或在地图内指定目标。图层下拉可显示独立外部先验；选择先验后禁用本机规划按钮。

工作端完成浏览器验证不代表车载已部署。原车载暂停/故障边界保留；下一次授权维护先同步当前规则、核验原SHA及数据路径，再做最小Studio重载和独立存活检查。不要为查看此面板唤醒或恢复旧探索。

## 2 创建传感器回放会话

`POST /api/platform/fieldmap/metric/sessions`，JSON如下。`source/epoch/frame_id/calibration_id`绑定到会话，后续扫描必须一致。`pose`是地图系内的**基座位姿**；`sensor_in_base`是固定传感器在基座内的平面外参。示例零外参只适用于合成或确实同原点的传感器。

```json
{
  "label": "已标定扫描的离线回放",
  "source": "example_lidar",
  "epoch": "example-reference-001",
  "frame_id": "map",
  "calibration_id": "example-only-replace-with-verified-id",
  "grid": {
    "width": 240,
    "height": 240,
    "resolution_m": 0.05,
    "origin_x_m": -6.0,
    "origin_y_m": -6.0
  },
  "sensor_in_base": {"x_m": 0.0, "y_m": 0.0, "yaw_rad": 0.0}
}
```

返回服务端生成的32位十六进制`id`与`revision=0`。未知字段拒绝；label最长80字符，标识最长96字符，地图16–400格/轴、2.5–20cm/格；传感器平移限制±2m、角度±π。此处范围是输入预算，不是物理可运动范围。

## 3 追加一帧

`POST /api/platform/fieldmap/metric/sessions/{id}/scan`：

```json
{
  "expected_revision": 0,
  "scan": {
    "source": "example_lidar",
    "epoch": "example-reference-001",
    "frame_id": "map",
    "calibration_id": "example-only-replace-with-verified-id",
    "sequence": 0,
    "stamp_s": 1.0,
    "range_min_m": 0.05,
    "range_max_m": 5.0,
    "pose": {
      "x_m": 0.0, "y_m": 0.0, "yaw_rad": 0.0,
      "stamp_s": 1.0, "quality": "tracked",
      "position_sigma_m": 0.02, "yaw_sigma_rad": 0.01
    },
    "rays": [
      {"angle_rad": 0.0, "range_m": 1.0, "hit": true},
      {"angle_rad": 0.4, "range_m": 5.0, "hit": false},
      {"angle_rad": -0.4, "range_m": null, "hit": false}
    ]
  }
}
```

角度是传感器内的水平角，距离m。第二束明确声明量程内无命中；第三束缺测，不清除空间。每帧1–720束，至少一束有效，所有有限数/范围/字段在写入前验证。位姿时间差≤100ms、位置标准差≤0.1m、角度标准差≤5°；只接受`tracked`。时间必须单调，sequence连续从0开始；与上一帧相比超过`1m/s×dt+0.15m`或`2rad/s×dt+0.15rad`的位姿变化被当成待重定位断点。这些是离线数据一致性门，不代替车载实际速度保护。

追加成功revision加1。单次穿越log-odds−0.4尚不达到自由阈值−0.5，第二份独立扫描才可能确认自由；单帧复制射线不会刷高置信度。量程端点被裁到地图外时不会在边框画假墙。每会话256帧，达到上限保留本会话并另建卷，不自动删除或拼接不同定位时期。

## 4 查看、预览与导出

- `GET .../metric/status`：明确显示当前为`known_pose_replay`、连续定位后端未连接、无实机权限。
- `GET .../metric/sessions`：列出保存会话、模式、帧数和先验数量。
- `GET .../metric/sessions/{id}`：返回栅格、计数、最后基座位姿、config、revision、先验元数据。`cells[y*width+x]`为左下原点：−1未知、0自由、50不确定、100占据。
- `POST .../sessions/{id}/plan`：`{"expected_revision":24}`自动选前沿；加`"goal_m":[1.0,0.0]`算目标路径；可选`robot_radius_m`为0.55–1.5m，默认0.6m。半径来自粗略收臂包络假设，未实测，不能减小来绕过未知阻挡。
- 规划同时返回`safe_centres`、候选观察位及路径；目标在膨胀后已知自由区，不能穿墙/切角/走未知。score使用前沿边界长度收益代理减路径长度，尚非基于完整视域的熵信息收益。
- `GET .../sessions/{id}/export`：可重放JSON，含原始扫描及独立先验；`occupancy.pgm`和`occupancy.yaml`配套导出，图像y翻转，未知/不确定均用205灰度。导出不会获得导航许可。

409表示revision冲突，重读后重新规划/提交；422表示输入/契约拒绝；403表示外来Origin；415表示不是JSON；413表示请求超预算；503表示存储失败，保留原数据库。普通JSON请求上限256KB，先验512KB。没有drive/torque/导航执行接口。

## 5 导入二维外部先验

`POST .../sessions/{id}/priors`接收`{"expected_revision":24,"format":"valetudo_v2","map":{...}}`。只接受`__class=ValetudoMap`、`metaData.version=2`、公开的`size/pixelSize/layers`契约 (S22/S37)。支持flat `[x,y]`像素与`[x,y,count]`横向RLE；单位cm→m，左上→左下，墙覆盖floor/segment，未知保持未知。

许多真机地图全局画布远大于实际房屋；实现先收集有界的观测像素，再裁剪到观测包围盒，保留`crop_pixels/source_canvas_cm`与转换后的外部原点。输出最多160,000格，解码像素预算640,000。不会按全局大画布直接分配巨型数组。

`GET .../sessions/{id}/priors/{prior_id}`返回独立外部层，始终`unregistered/navigation_use=false`。原始JSON及hash保存在独立表，不写入本机占据数组。P1没有自动SE(2)配准，也没有Dreame私有二进制decoder、实体/禁区/房间多边形导入；这些须按已验证协议版本另做adapter。当前只验证合成公开格式，没有取得/导入真实家中地图。

## 6 离线运行与恢复

在仓库根目录的PowerShell执行：

```powershell
.venv-debug\Scripts\python.exe -m deployment.fieldmap.metric_cli demo
.venv-debug\Scripts\python.exe -m deployment.fieldmap.metric_cli replay --input "<原输出卷>\mapping.json"
```

默认输出在忽略目录`.local-deploy/metric-replays/<新卷>/`，每次生成新的卷。包含`mapping.json`原始回放、`map.json`栅格、`plan.json`、PGM/YAML和SHA回执；SQLite也保留。恢复使用新session ID，记录原archive SHA与原session ID；不覆盖原输入文件/旧卷。可用`--output-root`选择私有存储位置。

CLI会完整校验并回放输入；失败保留已建的新卷用于诊断，不自动清理。有效导出/导入只是软件回放证明，不包含真实传感器精度、连续SLAM、动力学、动态避障或实机停车验收。
