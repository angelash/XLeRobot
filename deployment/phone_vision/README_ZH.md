# 手机 USB 视觉可行性探针

> 现已提供原 Studio 的“手机视觉”入口、受控后台采集及车载离线安装包。上车操作与当前验收范围见[接入与测试说明](../../docs/autonomy/PHONE_VISION_READY_20261109_ZH.md)。下文保留单次探针用法，便于脱离UI诊断USB链路。

只接收手机图像，按需调用既有 Studio 的图像质量接口；不打开机器人串口、不发运动指令、不建立独立网页或常驻服务。

## 安卓后摄

要求已授权 USB 调试的 Android 手机、可用的 ADB、**版本完全匹配的 scrcpy 5.0.1 server**，以及 Python 的 `av`、`opencv-python`、`numpy`、`requests`。从 [scrcpy 官方发布](https://github.com/Genymobile/scrcpy/releases/tag/v5.0.1)获取工具并按官方 `SHA256SUMS.txt` 校验；不要把自定义接收器与其他版本服务混用。

```powershell
python -m deployment.phone_vision.android_usb `
  --adb-path 'D:\androidSdk\platform-tools\adb.exe' `
  --server-path '<官方工具目录>\scrcpy-server' `
  --output '.local-deploy\phone-vision\android-test' `
  --duration 60 `
  --studio-url 'http://192.168.137.150:8770'
```

输出 1280×720／15fps 后摄数据。多设备时用 `--serial` 指定；可以用 `--camera-id` 指定已枚举的镜头。不传 `--studio-url` 时只做本地解码；传入时约每 15 秒向原 `/api/platform/vision/quality` 发送一张新帧，返回质量统计，**不会因此建立手机直播窗口或接入控制闭环**。不安装 APK，只运行本次采集的临时 scrcpy 服务；关闭音频和设备输入控制。

输出目录含 `result.json`、首张／最新画面及私有服务日志。结果区分源 PTS 和主机接收时间；接收帧间隔包含 JPEG 写入及同步质量请求的影响。源 PTS 单调和接收 FPS 不证明绝对传感器时延，HTTP 往返也不等于相机端到端延迟。USB 转发在本机回环使用，结束时关闭本次通道并删除本次独有临时服务文件；不重启 ADB 服务、不动其他转发。

## iPhone TrueDepth

要求 Apple USB 驱动、已信任电脑的 iPhone，以及手机端 Record3D 开启 USB Streaming 并实际采集。Windows Python 接收库可在隔离环境安装：

```powershell
python -m pip install record3d numpy
python -m deployment.phone_vision.iphone_usb `
  --output '.local-deploy\phone-vision\iphone-test' --duration 15
```

必须收到真实 RGB 和数值深度数组、存在有效正深度才标 `success=true`。只有 USB 枚举、配对正常或库成功导入均不算通过。脚本不代装手机 App、不购买、不读取相册或 App 内容、不改变配对。

`latest-rgbd-private.npz` 是原数组快照；几何配准、内参适用分辨率、有效距离、误差和绝对时间同步需要另外标定。此探针不会把数值深度直接交给控制器。

探针在每次帧回调中复制 RGB／深度／置信度数组，避免主线程分别读取时混入不同包；只保存首帧和末帧快照，减少持续压缩对接收的影响。输出包含回调频率、间隔、实际读帧数、深度数组变化数量、内参及有限正深度像素比例。`valid_depth_received` 只表示收到了有限且大于零的数值，不能解释为置信度或测距精度通过；置信度为空时明确保留空形状，不生成替代分数。

2026-11-09 手机准备后的独立复测已收到前置 TrueDepth、480×640 的 RGB 与 float32 深度：60 秒内1800次回调、约30fps，详见[实测报告续测](../../docs/autonomy/PHONE_VISION_TEST_20261109_ZH.md#7-iphone-准备后的续测真实-rgb-d-已通过)。仅保持 App 在前台且开启 USB 模式仍可能零帧，需要在手机上实际点开始采集。

## 验证与实测记录

```powershell
python -m pytest deployment/phone_vision/test_android_usb.py deployment/phone_vision/test_iphone_usb.py -q
```

协议检查使用生成字节，覆盖 scrcpy 5 的 session／config／keyframe 标志、分段读取、非法包长和中途断流。2026-11-09 实测见[落地报告](../../docs/autonomy/PHONE_VISION_TEST_20261109_ZH.md)。工具、虚拟环境、设备标识、原始画面和日志仅放忽略的本地目录，不提交公开仓库。

深度检查额外覆盖空数组、零／负／NaN／Inf 排除和分位数；全部使用生成数据，不打开手机或相机。

## 车载深度与局部点云采样

原 Studio 手机页启用iPhone后，可用隔离采集Python运行 `spatial_probe.py --output <新的私有目录> --wait 30 --samples 12`。默认只读取本机8770，不发送机器人命令。每组RGB／深度预览／float32原数组绑定同一新鲜会话与帧号，保留内参与文件哈希；收到真实数组后导出相机坐标PLY和主要平面描述。无帧时写失败回执，不生成地图。

0.15—1.5m只是分析筛选窗口，未验证距离精度或RGB-D配准；相机坐标尚无机器人外参。该工具不累计未配准帧、不提供SLAM或自动抓取目标。车载部署及USB故障边界见[2026-11-10续测](../../docs/autonomy/PHONE_VISION_DEPLOY_20261110_ZH.md)。
