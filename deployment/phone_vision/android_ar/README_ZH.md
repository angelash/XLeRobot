# K60 Ultra 本地 ARCore 验证端

此程序使用手机主摄与惯性传感器验证ARCore会话、自动深度、原始深度／置信度和位姿。它不读取相册、不控制机器人、不提供独立网站；输出保存在本App的外部私有文件目录，通过已有ADB读取。

已核验的构建依赖为官方ARCore SDK1.57.0 AAR，SHA-256 `df53bb8ea8e3a9f32e256b91909959d60b2e4c60d8fcc56b905b4c3e926f5c72`，来源 `https://dl.google.com/dl/android/maven2/com/google/ar/core/1.57.0/core-1.57.0.aar`。对应官方Google Play Services for AR安装包来自Google维护的GitHub Release `https://github.com/google-ar/arcore-android-sdk/releases/tag/1.57.0`，包名com.google.ar.core，实装版本1.57.262640093。

已有Android SDK34工具和JDK17时构建：

```powershell
python deployment/phone_vision/android_ar/build.py `
  --sdk D:/androidSdk --jdk 'C:/Program Files/Java/jdk-17' `
  --aar <已核验的core-1.57.0.aar> --output <新的私有构建目录>
```

更新已有本地安装时，增加 `--keystore <最初构建目录>/local-debug.keystore`，保持签名一致。APK、签名密钥、AAR／原生库及构建输出只放忽略目录，不提交Git；不要把本地调试签名当正式分发签名。Manifest显式包含SDK要求的最低AR服务版本、安装Activity和包可见性查询。

通过已授权ADB安装并启动 `org.xlerobot.arvision/.MainActivity`。首次相机权限需按手机系统要求授予，本轮限定为App前台使用；AR采集需要App前台与可跟踪的明亮、有纹理场景。运行时先停止同手机scrcpy后摄采集，避免相机竞争。屏幕会显示跟踪状态与深度组数，光照不足／初始化未完成会保留暂停状态。

每约500ms输出一次原子 `snapshot.bin`，同包包含JSON元信息、JPEG、自动深度uint16毫米数组、原始深度uint16毫米数组、原始置信度uint8。每个深度带独立原始时间戳，位姿为ARCore世界坐标，尚无机器人外参；跟踪暂停时不会伪造深度。原子包由[读取工具](../arcore_capture.py)解析，拒绝超限、截断及维度不一致，避免分别拉文件混入不同帧。

元信息导出SDK的深度归一化纹理坐标到CPU像素的四角映射。按[Google深度指南](https://developers.google.com/ar/develop/java/depth/developer-guide)换算采样坐标，不假设RGB和深度有相同视场。置信度只属于原始深度；[Frame API](https://developers.google.com/ar/reference/java/com/google/ar/core/Frame)明确原始深度可能重投影旧估计、自动深度也可能返回旧图，因此消费端还须检查各自时间戳。支持／数值存在／新鲜程度和绝对精度分别判断。

```powershell
python -m deployment.phone_vision.arcore_capture `
  --adb <adb.exe> --output <新的私有证据目录> --duration 60
```

工具只读取，不安装／启动App、不发电机命令。Wi-Fi已有调试信任时可加 `--serial <手机局域网地址:端口>`。成功要求至少5个TRACKING且有正深度的独立深度时间戳；即使通过，也不证明绝对精度、地图闭环、机器人坐标或自动避障安全。仅安装成功、支持查询true、RGB出图都不算深度实测通过。

Studio通过ADB forward连接App的Android本地抽象套接字，持续接收同样的有界原子包；没有对外监听的HTTP服务。每个接收会话使用独有套接字名，singleTop重连只替换数据通道，保留AR跟踪；明确停止时退出App并清本次转发。相机输出仍每500ms一组，手机端磁盘包保留供独立读取。启动时通过三次有往返边界的设备时钟读取估计保守采集时刻，保留不确定度，之后定期复核，避免每帧用两个ADB进程读文件／时钟造成积压。
