# 用户提供的 Jom MOSS 模型：对比、采用与转换

日期：2026-11-02。用户提供本地 `the-wandering-earth-moss-550w.zip`，要求与程序详细版比较，质量更好则采用。实际查看四个方向后，选择该模型作为新的默认材质版，同时保留程序详细版和简版。

## 文件与来源核验

原包 140,540,355 字节，SHA-256 为 `e57012024b29621e26268d5b0afb52a873af05f3ee17181687c09d03b8eda804`。ZIP 全部 CRC 通过；23 个文件为一个 FBX 和 22 张 4096×4096 PNG。原包只读保留，没有覆盖或改名。

实际解析得到 32,982 三角面、4 个网格/材质组（Main、Annexes、arm、Glass），具备 UV、法线，0 动画。它与先前调查的 [The Wandering Earth - Moss 550W](https://sketchfab.com/3d-models/the-wandering-earth-moss-550w-1f5fa5ff14514bfe89c6e2cf931d5dca) 源包字节数一致；本轮实时公开 API 返回作者 **Jom / jom239300891**、**CC Attribution**、[CC BY 4.0](https://creativecommons.org/licenses/by/4.0/) 链接。包内没有单独许可证文件，采用依据是作品公开许可及用户提供的对应包。

完整署名和修改声明随 [ATTRIBUTION.md](../../presentation/static/models/ATTRIBUTION.md)、[manifest.json](../../presentation/static/models/manifest.json) 和 GLB 内嵌元数据保留。展示页注明 Jom / CC BY 4.0，检视页提供来源和许可链接；GLB 导出保留同样的元数据。该模型不属于之前无法取得的 MakerWorld 924464，不称作电影官方原资产，也未用于训练模型。

## 对比判断

以相同的 Three.js 灯光、前侧视角、透视相机和最长边 3.15 的归一化尺度，对比上一版程序详细模型和该源包的转换版本，并查看正面、侧面和背面。

- 用户模型的壳体厚度、底部轮廓、侧轴连接、悬挂导轨和镜头结构更完整；法线贴图补充了微小凹槽、紧固件和面板起伏。
- 材质中包含现成的磨损、污渍、铭牌、警示标记和诊断面板；上一程序版只有简单材质和少量 Canvas 标签。这是本次最明显的质量差异。
- 用户模型只有 32,982 三角面、4 网格；程序详细版为 67,538 三角面、24 网格。前者几何更省，但依赖更多贴图，文件和解码内存更大，不能仅凭面数断言车载帧率更高。
- 玻璃、红色发光核心及屏幕透明部分已在导出的 GLB 上重新检查。当前为静态模型的展示微动，不具有可单独控制的真实机械关节或骨骼动画。

结论是采用用户模型作默认形象；旧程序版继续作为可选外观和视觉对照。此判断是人工视觉比较，不是可量化的电影还原百分比。

## 转换与体积控制

`presentation.prepare_moss` 用固定 Three.js 0.180.0 的 FBXLoader 解析 FBX，烘焙物体变换并居中，保留全部三角面；重新指定 PBR 材质，避免把 3DS Max 的未知材质直接作为网页材质。

- Main、arm、Annexes 纹理降至 2048，Glass 降至 1024；法线缩放后重新归一化。
- 颜色与发光图按 PNG 的 sRGB 标记处理；AO、粗糙度、金属度和法线保持数值数据，不按文件名猜测额外 ACES 转换。AO / roughness / metallic 分别打包到 RGB。
- **透明度先合入玻璃基础颜色的 alpha 通道**。本轮发现 GLTFExporter 不会自动导出独立 `alphaMap`；只设置 alphaMap 会在重新导入后盖住镜头，已修正。
- `presentation.optimize_glb` 仅重新打包内嵌图像与缓冲区偏移，不改动几何缓冲字节。7 张不透明基础颜色/ORM 图采用 JPEG quality 94、无色度抽样；6 张法线、发光与带 alpha 图保留无损 PNG。
- 自包含 GLB 有 13 张内嵌图像，不请求 CDN、外部纹理或解码器，不提高已有 20MiB 导入上限。

最终文件 [moss-550w-jom.glb](../../presentation/static/models/moss-550w-jom.glb)：**14,645,948 字节，约 13.97MiB**，SHA-256：

```text
a101d33e48898c2dddcf1a1b08c049c38c583794be9bfe6e3179d9e4cea6de06
```

原 ZIP、原始 FBX/4K 图、转换中间文件、比较截图与运行记录保存在 Git 忽略目录。公开 Git 只加入优化后的 CC BY 资产、署名、清单、转换工具与验收文档。

复现命令（开发工具需要 Pillow、NumPy、Playwright 和本地 Chromium；正常展示不需要这些开发依赖）：

```powershell
.\.venv-debug\Scripts\python.exe -m presentation.prepare_moss --archive PATH_TO_SOURCE_ZIP --work-dir .local-deploy/moss-user-asset --output presentation/static/models/moss-550w-jom.glb
```

工具会下载并验证固定 npm 工具包的 SHA-512，然后在临时 loopback 服务和隔离 Chromium 中转换；不上传模型或纹理。最终资产 SHA-256 以清单为准，重建环境差异可能影响图像编码字节。

## 展示接入与验证

新配置默认 `avatar.kind=licensed-moss`；设置可选择“Jom 材质版”“程序生成”或自定义 GLB/图片。已有配置读取不自动覆盖用户选择。检视页默认材质版，支持与程序详细/简版比较，四方向和拖动保持一致；材质版导出直接复制已核验 GLB，不二次降质。名称、人设、左右布局和收起居中继续可配置。

本轮另发现主展示页 `connect-src 'self'` 会拦截 ImageBitmapLoader 对内嵌图像 blob URL 的读取，而没有该页策略的检视页正常。已把该项精确调整为 `'self' blob:`，保留外部来源限制。浏览器回归增加对贴图、WebGL 和 CSP 控制台错误的检查，避免把“有 canvas 且未抛页面异常”当作完整材质通过；切换时释放纹理和 ImageBitmap。

验证证据在 `.local-deploy/presence-qa-v3/`：主界面/居中/四尺寸截图，Jom 四方向、程序四方向、新旧对比图、实际导出和 `verification.json`。转换源清单、原包与转换回执在 `.local-deploy/moss-user-asset/`。新增资产回归核对实际 SHA、大小、32,982 面、13 张内嵌图、署名和玻璃 alpha；打包测试比较几何字节、法线与透明图像的像素保留。

日常入口继续使用现有 Studio `8770` 的机器人形象页及 `/presence/`、`/presence/assets/model-inspector.html`；本轮不新增日常站点、端口或启动任务。工作端隔离浏览器检查不等于车载 GPU 帧率、温升或物理动作验收。

## 车载发布回执

已更新现有车载运行目录的39个展示文件，逐文件SHA-256核对；笔记本展示/资产测试 **23 passed**，工作端集成聚焦回归 **56 passed**，Node **3 passed**。保留名称、人设、布局、地图与复核数据，控制进程PID始终为8708。

初次车载测试缺少运行目录中的`deployment.onboard`，保留失败回执并恢复原文件，随后通过既有完整源码目录补齐测试的`PYTHONPATH`，没有向控制环境额外安装依赖。一次SSH内直接启动Studio在SSH退出后未继续存活，已通过既有`XLeRobot-Boot`任务恢复，并在SSH退出后重新验收；只恢复Studio，控制进程保持原PID，未新增任务、放宽防火墙或驱动电机。

从工作端通过`http://192.168.137.150:8770/presence/`及检视页实际加载材质版，1366×768截图已查看；贴图/WebGL/CSP页面错误0、外部请求0。HTTP取回完整14,645,948字节GLB，SHA-256与工程清单一致，当前配置为`licensed-moss`、名称MOSS。发布、失败、恢复、HTTP和真实页面证据保存在`.local-deploy/moss-studio-20261102/`。该浏览器运行在工作端，不冒充车载MX250帧率/温升测试。

资产与代码提交`5b3e7338d2ff62e781815614bc00d0d8c16db204`已推送并以远端SHA核对；笔记本完整源码工作区同步到该提交，24个变更文件经Git规范化内容核对全部一致，工作区干净，模型SHA一致。该源码同步回执与运行服务HTTP/浏览器回执分别保留。
