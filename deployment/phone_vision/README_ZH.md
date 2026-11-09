# 手机 USB 视觉可行性探针

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

## 验证与实测记录

```powershell
python -m pytest deployment/phone_vision/test_android_usb.py -q
```

协议检查使用生成字节，覆盖 scrcpy 5 的 session／config／keyframe 标志、分段读取、非法包长和中途断流。2026-11-09 实测见[落地报告](../../docs/autonomy/PHONE_VISION_TEST_20261109_ZH.md)。工具、虚拟环境、设备标识、原始画面和日志仅放忽略的本地目录，不提交公开仓库。
