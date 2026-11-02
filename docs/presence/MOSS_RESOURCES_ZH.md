# MOSS 资源核查

核查日期：2026-11-02。方法：GitHub REST API、固定提交 README/NOTICE、Sketchfab 公开模型 API。未登录平台、未取得授权下载包、未验证候选模型的实际外观或运行效果。检索不是完整资源清单。

## 可下载的三维候选

**The Wandering Earth - Moss 550W，作者 Jom**：

- [模型页](https://sketchfab.com/3d-models/the-wandering-earth-moss-550w-1f5fa5ff14514bfe89c6e2cf931d5dca)
- [模型公开 API](https://api.sketchfab.com/v3/models/1f5fa5ff14514bfe89c6e2cf931d5dca)
- API 标记可下载；许可为 [CC BY 4.0](https://creativecommons.org/licenses/by/4.0/)，要求署名；模型说明为 3DS Max 制作、Substance Painter 贴图。
- API 模型详情列 32,982 面、17,464 顶点、0 动画；搜索页的打包档案列导出后 34,234 顶点，两者口径不同，保留差异。
- GLB 包元数据：87,057,720 字节，13 张贴图，最高 4096；glTF 包约 85.7MB，原始包约 140.5MB。
- 判断：作为后续高精度候选，可经平台正规授权下载后做贴图压缩、减面和自包含 GLB 导出。首版不自动抓取网页预览缓存，不在未获取文件时宣称已导入；也不把 87MB 源包直接作为车载默认资源。
- 署名模板：`The Wandering Earth - Moss 550W — Jom / Sketchfab / CC BY 4.0；修改：减面、贴图缩小、格式转换（仅实际做过时填写）。`

这是开放许可的模型资产候选，不是带完整建模源工程的“开源代码”。该许可也不自动授予电影角色、名称和商标的第三方权利。

## 有模型文件但不能按仓库 MIT 直接复用

**[huyan1458/moss-pet](https://github.com/huyan1458/moss-pet)**：

- 核查提交：`c9d65f8f452419556b5806ea314465fec1696539`。
- 树中有 `assets/model/MOSS.blend`，2,702,043 字节；没有发现 GLB/glTF/OBJ/FBX。
- GitHub 仓库顶层检测为 MIT，但 [README](https://github.com/huyan1458/moss-pet/blob/c9d65f8f452419556b5806ea314465fec1696539/README.md) 和 [NOTICE](https://github.com/huyan1458/moss-pet/blob/c9d65f8f452419556b5806ea314465fec1696539/NOTICE.md) 明确限定：MIT 只覆盖原创脚本、文档和 Skill，**角色 PNG、模型、纹理及展示素材不在 MIT 内**。
- 结论：可以研究其公开的资源组织方式；本工程不复制模型、贴图、动画或精灵图。取得单独授权后，才能按实际授权范围决定是否使用。

## 其他参考

| 来源 | 已核实内容 | 本次处理 |
|---|---|---|
| [xylt/550W-MOSS](https://github.com/xylt/550W-MOSS) | DSH 皮肤；README 称手写 SVG/CSS 红瞳；许可证文件自述 MIT，GitHub 检测 NOASSERTION；未发现三维模型 | 界面参考，不复制代码或素材 |
| [star-reader/550W-MOSS-System-With-ChatGPT-Front-End](https://github.com/star-reader/550W-MOSS-System-With-ChatGPT-Front-End) | Vue3/Vite 前端；README 为 CC BY-NC-SA 4.0 并限制收费用途；未发现三维模型 | 历史参考，不混入本框架 |
| [Dawninest/550W_macOS_Saver](https://github.com/Dawninest/550W_macOS_Saver) | macOS 屏保；顶层 Apache-2.0；树中未发现常见三维模型文件 | 平台不同，不采用 |

## 首版采用方式

1. 本项目用几何体、材质、镜头和灯光程序生成低复杂度的 **MOSS 风格形象**，没有下载或挪用上述受限模型；明确这是风格化版本，不是电影原资产。
2. 显示名、人设和渲染器分别配置。允许导入用户自有或已授权的单文件 GLB，填写来源、作者和许可证备注；保留现有默认形象作回退。
3. 导入默认上限 20MiB；不接受从 GLB 内加载外部资源。高精度候选先在工程外取得合法文件，再离线优化；不把平台可下载标记当作已取得文件。
4. 首版不下载电影音频、字体或标识。MOSS 人设是本项目编写的表达配置，不冒充电影官方系统。

以上不构成完整权利清查，也未验证模型的纹理、坐标朝向、关节结构或视觉相似度。是否换用 Jom 的模型，等实际授权包和优化后的文件可检查时再定。

## 2026-11-02 后续：用户提供的 MakerWorld 924464 与详细版

用户提供 [3dgo 作品页](https://3dgo.app/models/makerworld/924464)。该页与 [MakerWorld 原页](https://makerworld.com/en/models/924464) 出现 Cloudflare 页面验证，本轮没有取得原始文件或许可，不能按“免费打印模型”推定开放复用。原作品未导入。

已实际查看公开官方多视图、光学细节和渲染图，独立编写详细版程序几何与 Canvas 标签，接入原框架并提供可导出的自包含 GLB。没有复用 `moss-pet` 中许可排除的素材；官方图片只保留在本地研究目录。来源、结构比对、真实模型预算和验证见 [详细版还原记录](MOSS_FIDELITY_ZH.md)。此前的首版调查与候选包“未取得”状态继续保留。
