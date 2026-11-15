# P1开发验证与交接

日期：2026-11-15。研究先行提交：`1169883`，已核验推送到`origin/main`。此文随其后的开发提交归档；实际开发SHA以Git日志为准。

## 实际交付

在原`deployment/fieldmap`和原8770应用增加已知位姿建图/规划模块：

- 有界概率占据栅格、同帧证据去重、命中优先、缺测/未知保留、图外截断与传感器固定外参。
- source、epoch、frame、标定、时间配对、协方差、位姿断点和连续sequence校验；拒绝整组输入时不留下部分更新。
- 整车保守圆包络膨胀，未知/不确定/图外不可走；可达前沿的观察位在已知自由一侧，不能隔墙获取“收益”；目标A*与前沿Dijkstra均禁止对角切角。
- SQLite事务、revision冲突拒绝、原扫描hash校验、有界缓存、重启重放；原空间照片、地图、复核和控制文件保持。
- Valetudo v2二维先验转换，cm→m、左上→左下、横向RLE、实际观测包围盒裁剪、墙优先。原文/hash单独保存，不融合进本机自由格。
- 原空间页中的算法回放面板、两种布局、独立先验图层、点击目标、PGM/YAML与原始JSON导出；离线CLI用新卷恢复导出，保留输入与来源hash。

连续SLAM后端、真实测距adapter、跨源自动SE(2)配准、Dreame原生二进制解码、真实房间分区/实体几何、实时动态成本层与轮控执行**尚未完成**。全部接口保持`motion_enabled=false / automatic_clearance=false`，回放不认证实时新鲜度。

## 软件与浏览器验证

工作端`.venv-debug`运行：

```powershell
.venv-debug\Scripts\python.exe -m pytest -q deployment/fieldmap fabrication/assembly/debug_console/test_platform.py
.venv-debug\Scripts\python.exe -m pytest -q deployment/fieldmap/test_metric.py deployment/fieldmap/test_metric_api.py
node fabrication/assembly/debug_console/test_platform_dashboard.cjs
.venv-debug\Scripts\python.exe -X utf8 deployment/fieldmap/verify_metric_browser.py
```

- 空间模块及平台完整相关回归：111 passed，113.60秒；这是开发中期树（当时含30项新映射检查）。
- 后续存储损坏分类、离线恢复、前沿碎片预算等修正后的映射专项：32 passed。与上项重叠，不相加宣称143项独立测试。
- 原平台Node页面检查通过；新JS语法检查通过。
- Microsoft Edge真实headless浏览器，1360×1000及390×844，两种布局无横向溢出；通过真实FastAPI处理器而非硬编码API响应。Playwright将请求交给TestClient，没有新增监听端口/开机服务。
- 浏览器创建24帧合成地图、选前沿、切换外部先验时禁用本机规划、切回、点击目标、重载后保留原栅格均通过。页面错误0、非允许请求0、核心循环大模型调用0、运动命令0。
- 文字与按钮检查后发现空状态遮罩没有隐藏，已修正并添加隐藏断言和非空绘图像素检查；再次查看桌面/窄屏截图确认实际栅格/坐标轴/路径可见。窄屏点击落点按10cm格与整数CSS像素精度校验，不把亚像素指针差异当作实物测距精度。

私有截图与回执：`.local-deploy/vacuum-mapping-20261115/browser/`。真实浏览器使用的是合成数据和原应用路由，**不是车载地址或实机效果验收**。

## 可重复的算法结果

24帧解析合成办公室扫描，默认160×120、0.1m/格：自由4,186格、占据149格、不确定1格、未知14,864格；产生18格点路径到可达前沿。栅格数组JSON的SHA-256：

`86d2e5455c83b0904105bc4977b8e3ece16b7794ebcd688fdfd01bcc974c868f`

两个新会话、导出后新卷恢复、UI创建及重载得到一致栅格；外部先验导入前后本机数组hash保持。自由格41.86m²只是合成回放栅格面积，不是房间覆盖率、实物成功率或家庭面积。

单次CLI建图与规划约1.44秒；一次浏览器生成与规划约2.71秒，均为工作端单次观察，不是P95统计或车载性能保证。P1每次预览重新计算几何前沿，尚未满足P2的实时更新目标；真实后端/滚动局部层需独立性能测试。CLI样本与按文件SHA回执在`.local-deploy/vacuum-mapping-20261115/replay-artifacts/`。

## 文献与资料归档验证

- `SOURCES.json`共37项候选，34项取得并逐项与本地最终原文hash核对；3项获取失败保留，未当已读正文依据。
- Crossref五组有界主题检索取得40条原始题录候选，保留最初429限流失败。包含评审附件、月球SLAM等噪声和仅预印本条目；未把题录数当成已读/纳入的论文数。技术选择依据来自研究入口中明确标注的原始论文、正式项目、规范和公开实现。
- 最终原文、提取文本/扫描页及题录/来源账本制成私有`public-research-sources-20261115.zip`：72个载荷成员另加MANIFEST，载荷4,964,494字节，ZIP2,535,690字节；精确成员集、逐成员长度/SHA-256与CRC读回通过。
- ZIP SHA：`7684d680a4a5e4b94f6e9e5971b7e52292418cfd4824facef445998695042215`。路径位于`.local-deploy/vacuum-mapping-20261115/`；这是本地归档，不宣称异盘独立备份。失败尝试保留元数据，原文快照以最终取得版本为准。
- HA调查回执独立保留，不加入这个公开来源包；没有发布HA token、账号标识、家庭布局、运行日志或现场照片。

## 实机状态与后续顺序

本次未唤醒车载、未部署到车载、未启动清扫/家电建图、未发机器人动作。最新交接的轮组异常、右肩物理hold及暂停边界保留；其他工作区的两处既存控制代码修改未纳入本次提交。

下次获准维护/恢复时先同步规则差异、查完整设备/停车/故障状态，再解决传感器尺度、时间、安装外参与连续定位输入。优先用真实只读录制回放验证SLAM Toolbox/RTAB-Map路线，测出精度、失跟踪和车载延迟；达标后才通过原8765执行有界建图，最后再做多房间与全覆盖。HA恢复后可以读取真实地图，但仍先放独立先验层，本机重验净空。
