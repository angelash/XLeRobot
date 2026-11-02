# XLeRobot Presence：机器人形象与过程展示

笔记本全屏展示：左侧可轻微转动的 MOSS 三维形象，右侧过程时间轴与视觉工作区；收起过程后形象居中。名字、图片/GLB 形象、人设、强调色、布局比例与动效都可修改并保存。默认是依据公开官方设定独立重建的 550W 详细版，也可选择首版简化形象。

设计与资源调查先于实现提交，见 [设计基线](../docs/presence/DESIGN_ZH.md)、[MOSS 资源核查](../docs/presence/MOSS_RESOURCES_ZH.md) 和 [事件协议](../docs/presence/EVENT_PROTOCOL_ZH.md)。

## 启动

在仓库根目录，复用现有 `.venv-debug`：

```powershell
.\.venv-debug\Scripts\python.exe -m presentation.server
```

打开 **http://127.0.0.1:8780/presence/**。默认只启动展示，不连接相机或引擎；可以点“播放演示”，或在“视觉工作区”分析本地图片。前端运行依赖已经随工程提供，无需 Node.js、npm 或 CDN。

现有 Studio 更新并重启后，也可从侧栏“机器人形象与过程”进入 **http://127.0.0.1:8770/presence/**。Studio 复用原端口与来源策略，自动读取本机 8781 消息桥的公开回执；只有点击“打开相机”才请求一路 8765 MJPEG。源码更新不会自动重启已运行的 Studio 或会话桥。

独立展示也可显式接本机服务：

```powershell
.\.venv-debug\Scripts\python.exe -m presentation.server --onboard-url http://127.0.0.1:8781 --controller-url http://127.0.0.1:8765
```

这些参数只接受 loopback HTTP 来源。车载机可直接在本机浏览器打开；工作端远程查看沿用已有 Studio 访问方式。此版本不自行修改防火墙、开机任务或机器人运行进程。

需要单独配置/媒体目录时，使用 `--data-dir .local-deploy/presence-preview/data`。默认路径是 Git 忽略的 `fabrication/assembly/debug_console/platform-data/presence/`；导入文件不加入 Git。

## 使用

- “收起过程”或 `I` 切换居中模式；“全屏”或 `F` 进入全屏；在输入框中不会触发这些快捷键。
- “设置”可以修改身份、问候、人设、形象与布局。GLB 必须为自包含 glTF 2.0 Binary，不依赖外部纹理或压缩解码插件，最多 20MiB。图片支持 PNG/JPEG/WebP，最多 10MiB、1200 万像素；会去除原始元数据并重编码。
- “设置 → 内置版本”切换详细版/简版；“多视角检视形象”进入 [形象检视页](http://127.0.0.1:8780/presence/assets/model-inspector.html)，支持正面、前侧、侧面、背面、拖动旋转、缩放和自包含 GLB 导出。此导出是静态形象，不含展示页的交互微动逻辑。
- “导出设置”包含人设文本与资源 ID，不打包模型或照片；跨机器导入后，要重新导入缺失的媒体文件。署名字段可保存作者、许可证和来源。
- 过程支持全部/摘要/工具/视觉筛选、展开较长输出、查看事件对应图片和“回到最新”。上滚、键盘或触摸滚动时停止自动跟随。
- “分析图片”做真实像素统计：亮度、暗部、高亮与 Laplacian 清晰度方差。它不做语义识别或目标检测。“放大原图”可查看实际输入图片。
- 相机直播与分析帧分开显示。当前 MJPEG 没有可信采集时间戳，因此标注源时间未知；不开三路自动预览，不将迟到的框叠在直播上。隐藏视觉区、关闭观看、隐藏页面或离开页面会释放观看连接。
- 演示使用本地生成的活动方块，所有事件标为“演示数据”；标注来自合成几何，不冒充模型识别。退出演示会回到真实事件窗口。
- 只有连接了既有会话桥，底部输入才可发送。展示页的消息携带当前表达设定，仍受桥原有观察/规划策略约束；不会启用运动。未知投递结果保留原 ID 查询，不自动重发。

## 工具与视频分析器接入

发布可公开的工具进度：

```powershell
.\.venv-debug\Scripts\python.exe -m presentation.client emit --file presentation/examples/tool-event.json
```

接 Studio 时加 `--url http://127.0.0.1:8770`。事件发布只允许本机显式生产者，不是机器人写接口。

图片/视频分析器先登记**实际分析的输入帧**：

```powershell
.\.venv-debug\Scripts\python.exe -m presentation.client media --file your-frame.png
```

用返回的资源 ID 发布 `vision` 事件，带 `frame_id`、真实采集时间（不知道则 null）、公开结果和可选归一化框。页面将结果固定在该帧上，直播照常显示当前画面。契约与示例见 [EVENT_PROTOCOL_ZH.md](../docs/presence/EVENT_PROTOCOL_ZH.md)。已有工具需显式接此发布接口；本版没有自动读取所有旧工具输出，也不读取私有引擎 JSONL。

历史最多保留 500 条事件；去重范围是当前历史窗口。进程重启或历史游标失效时重建展示窗口；不重放任务。媒体库最多 64MiB，满时明确拒绝新上传；原照片和 GLB 不会被自动删除或替换。事件内存不跨服务重启持久化，配置与导入资源会保留。

## 代码结构

- `state.py`：版本化配置、事件校验、历史窗口、图像与 GLB 校验、本地像素统计。
- `api.py`：HTTP/SSE、媒体、配置、固定来源代理；`adapters.py`：车载公开回执和消息接入。
- `static/avatar.js`：三维形象生命周期与 GLB/图片替换；`model.js`：有界客户端事件状态；`app.js`：界面交互；`demo.js`：独立合成数据源。
- `static/moss-detailed.js`：按材质合批的详细机身、光学镜头与悬挂几何；`static/model-inspector.*`：多视角检视、简版对照和 GLB 导出。
- `client.py`：显式发布 CLI；`server.py`：独立本机入口。
- `static/vendor/`：固定 Three.js 0.180.0，MIT 许可证与 SHA-256 清单；可用 `python -m presentation.vendor_three` 从核验过完整性的 npm 包重建。

内置模型由本项目代码生成。用户提供的 MakerWorld 924464 原文件因页面验证未取得，当前详细版依据公开官方多视图独立重建；未复制 `moss-pet` 的受限模型或贴图，也未导入 Sketchfab 的 87MB 候选包。来源、结构比对与实测体积见 [详细版还原记录](../docs/presence/MOSS_FIDELITY_ZH.md)。

## 验证

```powershell
.\.venv-debug\Scripts\python.exe -m pytest presentation/tests/test_presence.py deployment/onboard/test_onboard.py fabrication/assembly/debug_console/test_platform.py -q
node --test presentation/tests/test_model.mjs
.\.venv-debug\Scripts\python.exe presentation/tests/verify_browser.py
```

浏览器测试是可选开发工具，需要 Python Playwright 和已安装的 Chromium；生产运行不需要。它启动隔离的本机测试服务，使用生成的图片和三角形 GLB，检查三维渲染、布局、配置持久化、演示、图片分析、形象替换、SSE 与断线重连，并检查详细版四方向、GLB 导出再导入、简版切换。截图、测试配置和回执留在 `.local-deploy/presence-qa/`，不提交；可用 `--out .local-deploy/presence-qa-v2` 指定本轮证据目录。结果与实机待办见 [验收记录](../docs/presence/ACCEPTANCE_ZH.md)。
