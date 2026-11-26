XLeRobot PICO Neo3 VR 遥控安装包
================================

本包含车载 Studio 的 HTTPS 私钥（vehicle/studio-tls/server.key），只给机器人笔记本使用，不要外传或上传。
车载基线提交：{BASE}
机器人地址：{HOST}
本地根证书 SHA-256（在 PICO 上安装证书时核对）：
{CA_SHA256}

一、车载笔记本（需要维护窗口；不会启动接管、上扭矩或运动）
1. 把整个文件夹放到笔记本，例如 D:\Robot\pico-vr\
2. 只看计划：
   powershell -ExecutionPolicy Bypass -File vehicle\install-pico-vr.ps1
3. 安装并重启控制服务（VR 输入接口在 8765 里）：
   powershell -ExecutionPolicy Bypass -File vehicle\install-pico-vr.ps1 -Apply -RestartController
4. 放行 PICO（管理员 PowerShell，地址换成 PICO 在热点上的地址）：
   powershell -ExecutionPolicy Bypass -File vehicle\install-pico-vr.ps1 -Apply -PicoAddress 192.168.137.23
   失败会自动恢复原文件与原配置。

二、PICO Neo3
1. 开发者模式 + USB：  adb install -r pico\XLeRobotVR.apk
2. 资源库 → 未知来源 → XLeRobot VR：
   ① 检测连接  ② 安装机器人证书（核对上面的指纹）  ③ 打开 VR 遥控
3. 也可直接在 PICO 浏览器打开  https://{HOST}:8770/vr/

三、使用
先选“预演”进入 VR 熟悉映射；再选“实机”、勾选安全确认，进入 VR 后按住 X+A 1.5 秒开启，B 急停。
左摇杆开车，右摇杆转头，按住握把同侧手臂跟随，扳机控制夹爪，X/A 重新校准正前方。

完整说明：PICO_VR_ZH.md
