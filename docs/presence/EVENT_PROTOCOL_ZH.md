# 展示事件协议 v1

日期：2026-11-02。用于 Web 与未来 Unity 呈现端；事件只表达可公开进度和结果，不承载电机命令或模型内部推理链。

## 事件与连接

`POST /api/presence/events` 接收单条事件，返回补充了 `seq`、`epoch` 和接收时间的事件。生产者必须从本机调用；示例 CLI：`python -m presentation.client emit --file event.json`。同一 `event_id` 重传不重复追加；同 ID 不同内容拒绝。

首版实现补充（2026-11-02）：幂等覆盖当前保留的500条历史窗口，窗口外的事件ID不永久保存；服务重启也会更换epoch。生产者应只重传尚未确认的新近事件，不把旧窗口当作任务重放队列。

`GET /api/presence/state` 获取配置、连接状态和当前有界历史。`GET /api/presence/stream?after=42&epoch=...` 提供 SSE；浏览器断线携带 Last-Event-ID。进程重启或游标落在已淘汰历史之前时发送 `reset`，客户端重建当前窗口，不将旧任务重新执行。

```json
{
  "version": 1,
  "event_id": "vision-job-17-complete",
  "kind": "vision",
  "phase": "completed",
  "title": "图像分析完成",
  "text": "已完成此输入帧的分析。",
  "source": "vision-worker",
  "evidence": "reported",
  "task_id": "job-17",
  "frame_id": "head-000173",
  "media_id": "由媒体接口返回的ID",
  "captured_at": "2026-11-02T20:20:01.123Z",
  "overlays": [
    {"label": "工具箱", "x": 0.20, "y": 0.30, "w": 0.40, "h": 0.25, "confidence": 0.91}
  ]
}
```

- `kind`：`system / task / summary / tool / vision / message`。
- `phase`：`started / progress / completed / failed / cancelled / interrupted`。
- `evidence`：`reported`（生产者报告）、`computed`（本机算法计算）、`demo`（合成演示）。不根据一条 reported 事件自动升级为物理测试通过。
- `text` 是可公开文本，客户端只按文本呈现；不执行 HTML/Markdown 中的代码。发布者不得提交凭据、私有引擎诊断或内部推理内容。
- `captured_at` 是生产者提供的采集时间；未知用 null。服务接收时间单独记录，不回填为采集时间。
- `frame_id` 是分析输入帧标识；`media_id` 必须来自当前服务已登记的图片。`overlays` 必须同时有两者，否则拒绝；坐标为原图归一化值，不能越界。
- 无媒体事件仍能记录进度；无标注结果照常显示原始分析帧和文本，不虚构检测框。

## 媒体和实时视频

`POST /api/presence/media?name=...` 接收图像原始字节，返回媒体 ID、尺寸和地址；服务器解码校验、去除元数据并重编码，最多 10MiB。`POST /api/presence/avatar?name=...` 接收自包含 GLB 或允许的图片。资源使用随机 ID；客户端不能指定本机文件路径。

`POST /api/presence/analyze` 对媒体 ID 做本地像素质量统计，发布 started/completed/failed 视觉事件，结果绑定原图 ID。此接口不是通用语义识别。

现有相机直播走固定的 `/api/presence/camera/{0|1|2}` 代理，必须手动打开；没有源时间戳时标为未知。该直播本身不提供逐帧分析证据。外部视频分析器仍要上传实际输入帧，并按上面的 `frame_id` 发布分析事件；页面将其固定在“分析帧”区，不把框叠到时间不明的 MJPEG 当前帧。

## 配置与消息

- `GET /api/presence/config`；`PUT /api/presence/config`：版本化完整配置。`name / subtitle / greeting / persona / accent / avatar / layout` 分离；导出不含媒体二进制与凭据，迁移时需另带资源。
- `POST /api/presence/messages`：经固定上游提交给现有观察/规划桥，使用稳定请求 ID，携带当前表达设定；网络结果不确定时查询原 ID，不自动重发。
- 车载桥新增 `GET /v1/presentation` 仅返回最近有界消息的公开投影，供展示读回 queued/running/completed/failed/cancelled/interrupted；不返回诊断文件、引擎原始事件和认证配置。

## 有界与故障语义

首版目标上限：事件历史 500 条、单段文本 8000 字、媒体库 64MiB、单个 GLB 20MiB；媒体达到上限时明确拒绝新上传，不悄悄让历史卡片的画面变成另一张图片。前端时间轴也做有界保留。

配置故障保留原文件并显式报错，不静默覆盖；相机超时关闭连接并提示；SSE 重连只恢复展示，绝不重放工具或动作；事件来源断开时保留最后已确认内容，并显示连接失效。

生产者实例与示例画面均要标注 demo，真实事件与演示使用独立客户端状态；关闭演示立即返回真实事件窗口。
