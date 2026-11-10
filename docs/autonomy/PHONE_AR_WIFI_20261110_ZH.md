# 手机 ARCore 与 Record3D Wi-Fi 接入记录

日期：2026-11-10。入口沿用原 XLeRobot Studio 的 `/#phones`，后端仍在8770，控制仍由8765唯一所有者负责。本轮0电机目标、0扭矩写入、0底盘命令。工作端USB接红米，车载通过已有Wi-Fi ADB信任连接；iPhone使用用户指定的私有Wi-Fi地址。

## 当前验收结论

Google官方AR服务和可审查的本地AR采集App均已安装并独立回读哈希，K60 Ultra原生Session、自动／原始深度支持、真实深度数组、置信度和位姿均取得过实机数据。iPhone Wi-Fi的工作端与车载取流、原8770预览／数值接口已实测通过。

新增持续传输通道已部署并通过两端84项检查；这次更新后，红米系统相机权限变为未授予，iPhone原HTTP采集端点也离线，故**最终版本双机同时运行和AR模式完整启停／轮切尚未验收**。已请求在红米系统提示中允许前台相机、重新打开Record3D Wi-Fi采集。不能用较早版本的成功结果代替这一步，不能称已经具备可靠抓取测距、空间SLAM或自动避障。

## 已安装软件与来源

- Google Play Services for AR：包名 `com.google.ar.core`，实装版本 `1.57.262640093`。来源为[Google维护的ARCore SDK 1.57.0 Release](https://github.com/google-ar/arcore-android-sdk/releases/tag/1.57.0)。APK为87,748,272字节，SHA-256 `0828abd075ca69b15babc2a0216ebf8b9e119b50d3d4252326874b02e0e717c5`。apksigner验证Google证书SHA-256 `6e913f65ea53321ead6932ff777e1b7f94bbba7e2b0dafdd8494b100101e3797`。
- 本地AR采集App：包名 `org.xlerobot.arvision`，版本0.1／code1，最终私有构建v8。APK独立从手机拉回，SHA-256 `10578bee9e718043723b254d5fbfe0da337d5a1899bdbf3f0a458c73f937a1ad`与构建产物一致。源码与构建方法见[Android采集端](../../deployment/phone_vision/android_ar/README_ZH.md)。本地调试签名仅供当前测试，密钥不发布。
- 官方ARCore AAR 1.57.0：来自Google Maven，SHA-256 `df53bb8ea8e3a9f32e256b91909959d60b2e4c60d8fcc56b905b4c3e926f5c72`。使用已有Android SDK34／JDK17构建，不引入另一套机器人服务。
- Record3D Wi-Fi接收：独立便携Python包含aiortc1.14.0／PyAV16.0.1，保留原数值库，不升级控制器环境。依赖清单1771文件留私有回执。未采购新硬件或代购软件。

首次AR安装曾多次被系统以 `INSTALL_FAILED_USER_RESTRICTED` 拒绝；用户明确开启USB安装后成功。本次更新后相机权限又显示 `granted=false`，安装包正确不等于系统已允许相机。一次ADB停止应用与设置权限的组合命令被自动审批拒绝（`blocked by policy`，无更具体原因），改为请求用户通过手机系统提示授权。

## iPhone Wi-Fi 实测及精度边界

Record3D手机端提供HTTP信令和WebRTC视频：读取 `/metadata`、`/getOffer`，提交 `/answer`；接收端不配置公共STUN／TURN、不接收音频。私有配置只接受明确的RFC1918局域网IPv4 HTTP源。

视频左半为色相编码深度，右半为RGB，按发布者示例 `depth_m = 3 × hue` 还原。深度是压缩视频的近似解码值，显式记录 `depth_source=record3d_hue_video`、`sensor_raw_depth=false`，无置信度，不补造镜头类型。元信息中的原始尺寸480×640，实际单半视频曾出现180×240／240×320／360×480／480×640，内参逐帧随分辨率缩放。

- 工作端独立接收180帧，179张不同RGB。
- 车载65秒观察178组不同RGB与编码深度，中位样本龄130.5ms；该窗口也观察到另一个安卓彩色源。
- 第一次停止重启在25秒内未恢复，后续恢复用35.375秒、3次尝试。另35秒108轮询全部live、单一会话，不能外推无限长时或秒连。
- 8帧相机坐标点云探针通过，`registered_map=false`；没有外参、房间地图闭环或绝对距离标定。
- 最后一次更新后HTTP端点不通：车载有超时与连接被拒绝，保留原失败回执并等待手机重新开始采集。

样本龄包括本地接收／队列估计，不是已验证的光学端到端时延；Wi-Fi色相深度不能沿用历史USB TrueDepth的原始数组结论。

## 红米原生深度实测及质量

原生返回 `SUPPORTED_INSTALLED`，`AUTOMATIC`和`RAW_DEPTH_ONLY`均支持，使用0号主摄。最初RGB实图为暗景，`PAUSED/INSUFFICIENT_LIGHT`、0深度；用户对准明亮区并缓慢移动后，独立20.406秒取得40组TRACKING且有正深度的样本、40个不同的自动深度时间戳。

该组深度160×90；CPU RGB640×480，内参由ARCore读取，相机位姿为ARCore世界／OpenGL坐标。最后一帧自动深度正值100%，中位19.758m、最大48.003m；raw正值99.16%，但raw仅1个估计时间戳，不能计成40次独立测量。原始置信度中位0、最大192，达到128的像素仅0.3611%。这些值只证明API输出存在，**这组室内深度质量不足以用作抓取或导航距离依据**。

后续也实测到自动深度长时间复用旧时间戳，以及新会话全零深度。支持查询、数组非空、正数比例、置信度和精度必须分开。K60原镜头枚举未见Camera2 `DEPTH_OUTPUT`；本次ARCore算法深度并未证明新增硬件ToF传感器。

## 配准、时间和生命周期处理

[Google深度指南](https://developers.google.com/ar/develop/java/depth/developer-guide)用 `TEXTURE_NORMALIZED` 与 `IMAGE_PIXELS` 互相转换深度／CPU像素。真机导出的四角为 `(0,60)/(640,60)/(0,420)/(640,420)`：深度对应CPU彩色中间640×360区域。接收器按SDK映射裁剪采样，更新焦距／主点，再共同旋转RGB和深度；不把4:3彩色全图拉伸到16:9。陌生的非轴对齐、反射、越界映射拒绝，尚未做物理配准误差验收。

[Frame API](https://developers.google.com/ar/reference/java/com/google/ar/core/Frame)说明自动深度可能返回旧图，raw可能按当前位姿重投影旧估计；raw的时间戳变化才表示有新信息。接收器保留独立时间戳、raw估计龄，raw置信度只对应raw，不能套在自动深度图。CPU彩色与深度的Image时间戳比较；不假设Frame时间戳一定等于系统启动时钟。

深度落后超过800ms或全零时，仅提供新鲜RGB并隐藏深度，当前帧数组／像素接口拒绝返回旧深度。所有帧仍受原1秒源龄、会话和帧号保护。位姿保留其AR世界坐标标签，跟踪暂停时标为无效，不自动变成机器人定位。

最初每帧分别用ADB拉文件和设备时钟，车载测得合计约0.6—0.9秒开销，35／40秒窗口出现无可用安卓帧，未放宽时效门。最终改为本App的Android抽象套接字，经本次独有ADB forward持续传送有界原子包；没有新用户网站或开机服务。三次设备时钟读取保留往返不确定度，按最早可能采集时刻估龄，定期复核。接收进程失败只清本次转发，保留AR会话；明确停止／模式切换才退出本App。手机端仍保留原子磁盘包，便于独立回读。

原8770手机页提供ARCore／彩色模式、镜头与旋转选择、深度预览及像素估计读数，区分自动深度与原始置信度。最终UI仍待实际授权恢复后的浏览器和双机运行核验。

## 验证、部署和证据

本机及车载各84项检查通过，涵盖编码深度语义、动态内参、SDK裁剪、过期／全零回退、原子包边界／截断、套接字分段及超时、保守时钟、不重置原生AR会话的错误清理、原8770接口／启停规则；Java编译、APK签名验证及JS语法通过。这些生成数据检查不替代手机实测。

19个源码／文档文件分两次核对现有SHA、备份、原子替换；最终持续传输更新包SHA-256 `83c5ea24f87fb64c86d8f9ffbc56818e0b1a65e5a775f2b9128294d391e9c0e8`。仅重载原Studio，控制PID35476未变。原数据数量0／0／0、地图119观察／11特征保留；最初16电机／扭矩全关／轮零速，后续变为ID1读取失败／0电机，根因未确认，未发机器人命令。

私有证据位于工作端忽略目录 `.local-deploy/ar-wifi-20261110/`，关键回执：`installed-assets-v8.json`、`arcore-physical-success-check/result.json`、`car-iphone-wifi/result.json`、`iphone-restart-second.json`、`iphone-current-stability.json`、`iphone-spatial.txt`、`ar-latency.txt`、`alignment-car-tests.txt`、`stream-car-tests.txt`、`studio-dual-first/result.json`、`studio-dual-stream/result.json`、`stream-logs.txt`。原图、数组、个人内容、地址、APK、密钥与日志不入公开Git。

后续必须在手机权限与采集恢复后，验最终持续通道的真帧、数值／置信度质量、双机同时工作、启停／镜头释放、断流恢复；再做固定安装、已知距离对照与机器人外参。未通过这些步骤前不把新深度交给自动抓取或底盘避障，也不宣称空间建图完成。

## 当晚停测与明日准备

用户随后明确两台手机已锁屏，要求笔记本休眠、工程和资料备齐明天再测。21:52已停止两机采集和自动重试，各机器人任务均非活动；当前电机遥测0，未宣称重新取得停车证明。手机保持锁屏，不继续启动采集或索要当前测试条件。

[明日测试顺序](PHONE_NEXT_TEST_20261111_ZH.md)已准备并同步车载，11份私有材料共196,989,920字节逐项核对SHA、生成NEXT_TEST_KIT.json；现有安装和便携环境保留，明天无需重新寻找安装文件。21:56再检查任务和车载三份交付材料哈希后启动Windows休眠进程，随后SSH／8765／8770离线；未收到Windows退出码／电源事件，准确状态为“已发起休眠，主机随后离线”。最后文档在工作端保存并推送，车载唤醒后再同步收尾记录。
