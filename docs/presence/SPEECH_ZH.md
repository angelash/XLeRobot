# MiniMax 语音播放与声线试听

日期：2026-11-02。用户要求从 `F:\workspace\github\live-avatar` 获取 MiniMax 接入能力，为机器人形象增加语音播放；默认关闭，文字照常输出；寻找 MOSS 方向声线，找不到则提供全部声线逐个试听。

## 当前使用方式

现有 Studio 8770 → 机器人形象与过程；[直接进入声线试听](http://192.168.137.150:8770/presence/?voices=1)。沿用同一服务和页面，不新增站点、端口、S2S 管线或开机任务。

- 顶部“语音关闭”切换自动播报，初始值 **关闭**，也可在角色设置中配置开关与播放音量。开启后只朗读新完成的公开回复，文字始终保留；失败只提示语音状态，不影响任务和文字。
- “声线试听”列出当前账号接口的完整目录，可按语言、名称、描述和 `voice_id` 搜索，筛选 MOSS 方向候选，导出完整 JSON 清单。
- 选中一条声线后点“试听选中声线”；可编辑同一段样文，调整语速、音调、自然/平静情绪。首次合成后的相同文本和参数复用本机缓存；不预先合成全部332条。
- “使用此声线”保存声线与参数，**不打开自动播报**。停止试听、关闭开关、离开或隐藏页面会停止当前播放并取消尚未完成的请求；演示和旧历史不自动播报。
- 浏览器需要用户手势才能允许声音。已有配置为开启但新页面尚未获得播放许可时显示“启用声音”，点击后才为新回复合成。刷新、重连、重复事件与开启开关不会补播历史。

声音由打开展示页的浏览器播放。笔记本展示页用笔记本的音频输出，工作端远程打开则用工作端的音频输出；本轮验证了浏览器音频播放结束状态，没有把它称为车载实体扬声器响度或声学实测。

## 声线调查结果

先读取集成 `aigw-mediakit-tts` 的公开系统表（231项），再按 live-avatar 的实际 MiniMax 账号及区域查询 `POST /v1/get_voice`、`voice_type=all`。**2026-11-02 实际返回332个系统声线，克隆/设计声线均0。没有 MOSS 或550W同名声线。**当前完整公开目录随 [voices.json](../../presentation/voices.json) 归档；页面还可刷新当前账号目录，来源失败时明确显示快照/上次目录，不冒充实时结果。

官方参考：[声线列表](https://platform.minimaxi.com/docs/faq/system-voice-id)、[Get Voice 接口](https://platform.minimaxi.com/docs/api-reference/voice-management-get)。没有发现专门的官方 MOSS 声线，未进行电影配音克隆。以下五项按声音描述筛选作比较，不能据名称或合成成功断言与电影声音相似：

- **沉稳高管**：`Chinese (Mandarin)_Reliable_Executive`，冷静可靠的中年男声；暂作默认候选，播放仍关闭。
- **机械战甲**：`Robot_Armor`，电子机械质感男声。
- **播报男声**：`Chinese (Mandarin)_Male_Announcer`，清晰、有权威感的播报男声。
- **电台男主播**：`Chinese (Mandarin)_Radio_Host`，作为平稳叙述方向比较。
- **抒情男声**：`Chinese (Mandarin)_Lyrical_Voice`，可对比语速、音调后的表现。

实际合成了五份相同样文 WAV，时长依次约5.56、6.33、7.91、4.97、7.43秒，16kHz单声道PCM16；没有代用户选定最终声线。完整目录没有限制为这五项或旧仓库中原先的五种女声选择。

## 接入来源与实现

参考 `live-avatar` 提交 `fbf1111` 的 `integrations/s2s/minimax_tts_handler.py`：保留 MiniMax T2A v2 SSE、hex PCM、`base_resp`错误检查、`exclude_aggregated_audio`、中断和“只在首块音频前对网络错误重试一次”的做法。适配后的 [speech.py](../../presentation/speech.py) 不依赖其 S2S/LiveTalking 基类；MIT许可保留在 [LICENSE_LIVE_AVATAR.txt](../../presentation/LICENSE_LIVE_AVATAR.txt)。原仓库只读取，没有修改或重启它的运行服务。

使用原仓库已有的国际站 `https://api.minimax.io/v1/t2a_v2` 与 `speech-2.8-turbo`。显式配置工具只解析指定 `scripts/config.local.bat` 的 MiniMax set语句，不执行BAT：

```powershell
.\.venv-debug\Scripts\python.exe -m presentation.configure_speech --live-avatar F:\workspace\github\live-avatar
```

配置写到 Git 忽略的展示数据目录 `speech-provider.json`，API密钥只在服务端。迁移到笔记本通过既有严格主机校验的SSH传输到相同私有数据目录，没有把密钥写入源码包、命令文本、公开API或Git。也可由启动环境设置 `MINIMAX_API_KEY`、`MINIMAX_TTS_BASE_URL`、`MINIMAX_TTS_MODEL`。

`Profile.speech`保存开关、voice_id、语速、音调、音量与情绪，老配置缺该字段时默认关闭，读取不会覆写旧文件。朗读路由只接受服务端已有的`epoch/event_id/part`，只取`message/completed`的真实公开回复，不朗读工具日志、演示、私有推理或调用任意外部地址。去除代码块与链接地址后按句拆为最多220字符的片段，顺序合成播放；完整文字不删改。试听另走显式路由，最多500字符，与自动播报独立。

WAV缓存只保存生成音频，32MiB上限，只清理自身哈希命名的缓存文件；不清理用户图片或GLB。前端全程按同源请求获取音频，CSP增加`media-src 'self' blob: data:`以允许本地WAV及播放解锁。没有把MiniMax密钥送到浏览器。

## 验证与车载发布

- 工作端最终聚焦 Python 回归 **71 passed**，包含语音9项和配置兼容6项；前端事件/语音 Node **7 passed**，语音队列新增4项；原有四尺寸模型/布局/消息/图片/重连浏览器回归通过。
- 独立浏览器测试用生成WAV和模拟供应商，验证332条目录、候选/搜索/窄屏、选择保存、默认关闭无合成、试听不启用、新回复播报、关闭后文字继续、刷新重连不重复，以及停止后迟到音频不得播放。
- 实际 MiniMax API 查询332项，并合成上述五候选；实际PCM封装与时长读回通过。此项与模拟UI测试分别记录。
- 车载30项展示/语音测试通过，15个发布文件逐项SHA-256核对；只恢复现有Studio进程，沿用`XLeRobot-Boot`，8765控制进程PID8708保持，未发送实体动作或改变扭矩。角色、形象、布局和地图保留，交付开关为关闭。
- 从工作端实际浏览器打开车载8770，读取332条实时目录并试听“机械战甲”；音频播放至6.327438秒并结束，复用预置候选缓存，试听前后完整Profile一致、语音关闭，页面/渲染错误0。静态运行资源HTTP哈希读回一致。
- 最终同步包含空白试听文本校验和`?voices=1`直达试听库；随后真实浏览器从直达链接再次通过目录、完整试听和配置不变核验。最终发布恢复目录为车载`.local-deploy/presence-speech-20261102-145756`。

发布中第一次PowerShell5.1把无charset的JSON中文误解码，完整配置写回后内容比对检查阻止交付。失败文件保留；首次部署没有既有profile文件，按当时实际使用的源码默认值恢复角色配置；随后改为显式UTF-8字节解码读回和写回，复核角色、人设、形象、布局后发布通过。失败、恢复和成功均保留在本轮私有证据中，未静默忽略该失败。

证据目录：`.local-deploy/presence-tts-20261102/`（目录查询、真实样音回执、发布/恢复/远程页面）、`.local-deploy/presence-tts-qa/`（语音UI）、`.local-deploy/presence-tts-regression/`（原框架回归）。密钥、试听缓存、运行配置、截图和临时日志不提交。
