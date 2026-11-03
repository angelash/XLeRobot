"""Build the public journal from reviewed notes; no chat-store or robot access."""
from datetime import date, timedelta
import argparse
import json
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "docs/journal"


def bullet(items):
    return "\n".join("- " + item for item in items)


def source_links(ids, parent=""):
    return "、".join(f"[{sid}]({parent}SOURCES.md#{sid.lower()})" for sid in ids)


def build():
    data = json.loads((ROOT / "data.json").read_text(encoding="utf-8"))
    source_ids = {source["id"] for source in data["sources"]}
    assert len(source_ids) == data["snapshot"]["threads"]
    known_days = {day["date"]: day for day in data["days"]}
    assert len(known_days) == len(data["days"])
    calendar = []
    current, end = date.fromisoformat(data["range_start"]), date.fromisoformat(data["range_end"])
    while current <= end:
        key = current.isoformat()
        calendar.append(known_days.get(key, {"date": key, "title": "未找到当日记录", "status": "记录空白", "summary": "归档来源中未找到这一天的对话或台账条目；这不代表当天没有活动。", "sources": [], "progress": [], "problems": [], "next": [], "lessons": [], "commits": [], "evidence": [], "empty": True}))
        current += timedelta(days=1)
    assert set(known_days).issubset({day["date"] for day in calendar})
    output = {}
    for day in data["days"]:
        assert set(day["sources"]).issubset(source_ids)
        for evidence in day["evidence"]:
            if not (REPO / evidence).exists():
                raise ValueError(f"Missing evidence path: {evidence}")
        for commit in day["commits"]:
            subprocess.run(["git", "rev-parse", "--verify", commit + "^{commit}"], cwd=REPO, check=True, capture_output=True)
    for day in calendar:
        paragraphs = [f"# {day['date']} · {day['title']}", "", f"阶段：{day['status']}。时间口径：北京时间；本批对话截止 {data['cutoff']}。", "", day["summary"], ""]
        if day.get("empty"):
            paragraphs += ["请保留这个空白，后续找到原始资料后再追加。不要把前后日期的工作倒填成当日事实。", ""]
        else:
            for heading, key in [("当天推进", "progress"), ("问题与失败", "problems")]:
                paragraphs += ["## " + heading, "", bullet(day[key]), ""]
            paragraphs += ["## 当日结果与边界", "", day["outcome"], "", "## 后续事项", "", bullet(day["next"]), "", "## 可复用经验", "", bullet(day["lessons"]), "", "## 来源与证据", "", "会话：" + source_links(day["sources"], "../") + "。", ""]
            paragraphs += [bullet([f"[{path}](../../../{path})" for path in day["evidence"]]), ""]
            if day["commits"]:
                paragraphs += ["关键历史提交：" + "、".join(f"[{commit}](https://github.com/angelash/XLeRobot/commit/{commit})" for commit in day["commits"]) + "。", ""]
            paragraphs += ["以上是历史复盘。设计、软件验证、发布与实物结果按原记录分别表述；待办不是新的动作授权。", ""]
        paragraphs += ["[返回日期索引](../README.md) · [经验整理](../EXPERIENCE_ZH.md) · [覆盖范围](../COVERAGE.md)", ""]
        output[f"daily/{day['date']}.md"] = "\n".join(paragraphs)
    sources = ["# 会话来源目录", "", f"应用当前可见：XLeRobot 7 条全部纳入；open-share 9 条中纳入关联的 5 条。对话截取至 {data['cutoff']}（北京时间）。", "", "来源编号只在本日志内使用。thread ID 是稳定回查标识；日期按每条消息的 UTC 时间转换，不按会话创建日归档。", ""]
    for source in data["sources"]:
        sources += [f"<a id=\"{source['id'].lower()}\"></a>", "", f"## {source['id']} · {source['title']}", "", f"- 工作区：{source['workspace']}", f"- 会话：`{source['thread']}`", "- 纳入原因：" + source["reason"], f"- 私人阅读稿：`.local-archive/conversations-20261103/transcripts/{source['thread']}.md`（仅本机备份内）"]
        if "parent" in source:
            sources += [f"- 分叉来源：{source['parent']}；继承前缀 {source['inherited_records']} 条记录 / {source['inherited_bytes']:,} 字节。父文件保留原字节，按该边界恢复继承阅读；日报只计各会话自身新增消息。"]
        sources.append("")
    sources += ["## 辅助材料", "", "使用 PROJECT_STATE.md、PROJECT_HANDOFF_ZH.md、观察手册、待审核经验、Git 历史及已经保存在本机的证据交叉核对。它们是特定时点的记录，不代表本轮重新运行了机器人。", "", "## open-share 未纳入的当前会话", "", "当前可读的其余 4 条分别为开源游戏目录、视频转写、通用工程提交和视频修复工具讨论，未发现需要作为本机器人日志主线收录的内容。旧版历史索引的文件缺口见覆盖说明。", ""]
    output["SOURCES.md"] = "\n".join(sources)
    lessons = ["# 从 XLeRobot 制作过程整理出的经验", "", f"范围：2026-10-20 至 {data['range_end']}；对话截止 {data['cutoff']}（北京时间）。这是面向分享的复盘稿，保留适用范围与失败，不替代当前机器人控制规范。", ""]
    for number, lesson in enumerate(data["lessons"], 1):
        lessons += [f"## {number:02d} · {lesson['title']}", "", "发生日期：" + "、".join(lesson["dates"]) + "。", "", "遇到的问题：" + lesson["problem"], "", "后来采用的做法：" + lesson["method"], "", "适用边界：" + lesson["limit"], "", "来源：" + source_links(lesson["sources"]) + "。", ""]
    lessons += ["## 可展开的分享叙事", "", bullet(data["story_outline"]), "", "## 分享前保留的事实边界", "", bullet(["一次插头抓取成功有三路视觉依据，提升高度为估计，尚无重复成功率统计。", "快速抓取在本快照截止时只可确认代码与离线验证，以及用户后续部署陈述；不能写成新流程已完成实机验收。", "单目避障仍缺地面标定与 O1–O5 实测；地图为观察记录，没有完成自主导航。", "历史价格、规格与采购建议不是当前推荐，也不代表设备已购买。", "本稿不包含账号凭据和现场原图；如后续加入照片，先检查人物、屏幕与资产使用条件。"]), ""]
    output["EXPERIENCE_ZH.md"] = "\n".join(lessons)
    stats = data["snapshot"]
    coverage = ["# 本次归档的覆盖与备份边界", "", f"对话采样完成：{data['cutoff']}（北京时间），对应原始 UTC 记录截至 2026-11-03 06:46 左右。支持材料逐文件复制，非跨文件原子快照。", "", "## 已保留", "", bullet([f"12 条会话：本工作区全部 7 条、open-share 关联 5 条；共 {stats['turns']} 个本会话回合、{stats['messages']:,} 条去重用户/助手消息，其中用户消息 {stats['user_messages']} 条。", f"12 份原始 JSONL，合计 {stats['raw_bytes']:,} 字节。原始工具记录、行内图像和分叉元数据保留；阅读稿仅提取用户/助手文本。", "2 条分叉验证父前缀的字节位置与记录数量。单独会话阅读稿恢复继承；按日证据不重复计入继承消息。", "33 个用户图片引用：27 个原文件复制并核对哈希；6 个原路径已失效，从原始会话中恢复当时实际送入会话的编码图，并验证可以解码。不能保证这 6 张与原文件逐字节相同。", "补充保存 863 个已有本地证据文件，包括两轮抓取的 474 张运行照片、计划/记录、项目台账/规范快照，以及 10 月 29 日的旧备份包。", "15 个连续日期的日报，其中 9 天有来源记录、6 天显式留白。日报是整理稿；私人 daily-evidence 是用户消息和回合末回复原文摘录。"]), "", "## 未宣称覆盖的内容", "", bullet(["旧版历史库仍有 129 条 open-share 索引指向失效路径。当前 XLeRobot 索引无选中会话缺失；旧 open-share 全部历史内容未恢复，不能排除旧会话中还有未发现的相关片段。索引、原路径和失败项保存在私人 discovery.json。", "没有把其他项目全部对话、整套工作目录、虚拟环境、账号配置或私钥纳入备份。", "没有重新向机器人请求实时状态、遥测、相机或运行记录。车载未同步到工作电脑的新增文件、其他外部二进制附件不在本次补充证据范围。", "S07 在采样末尾收到用户“已部署，可以测试”的新消息，当时尚在预检。本批日报保留这一状态，不追写截止后的实机结果。S12 的最终交付同样晚于原始快照。", "原始记录包含凭据或私人画面，仅在 Git 忽略的私人快照和 E 盘备份中；公开文档是经整理的文本。", "旧经验文件出现重复 L-032 编号；本次不重写历史经验，引用时以文件、日期与标题一起辨认。"]), "", "## 存储与验证", "", f"私人快照：`{stats['private_snapshot']}`（F 盘项目内，Git 忽略）。", "", f"E 盘备份：`{stats['backup']}`。ZIP 同目录的 `.receipt.json` 与 `.zip.sha256` 给出文件数量、验证时间和完整哈希；实际交付回执见 [BACKUP_RECEIPT.md](BACKUP_RECEIPT.md)。", "", "备份封存前给每个文件记录长度与 SHA-256；封存后从 ZIP 逐文件解压到内存校验，并核对 ZIP 路径集合。原始源文件按完整 JSONL 行截取，只读复制，不修改聊天数据库。", "", "这是同一台电脑上的跨盘/分区备份，加上公开日志的 Git 版本管理；不是异机灾备或网盘备份。", ""]
    coverage_text = "\n".join(coverage)
    coverage_text = coverage_text.replace("这是同一台电脑上的跨盘/分区备份，加上公开日志的 Git 版本管理；不是异机灾备或网盘备份。", f"另有独立物理硬盘副本：`{stats['secondary_backup']}`。本机核验 E/F 同属物理磁盘 1，G 属于物理磁盘 2。公开整理稿另由 Git 版本管理保存；本轮没有上传网盘。")
    output["COVERAGE.md"] = coverage_text
    readme = ["# XLeRobot 每日制作日志", "", "用于每天回看进展，也为后续经验分享保留可回查的依据。", "", "[打开离线阅读页](index.html) · [经验分享稿](EXPERIENCE_ZH.md) · [会话来源](SOURCES.md) · [归档覆盖说明](COVERAGE.md) · [备份回执](BACKUP_RECEIPT.md)", "", f"本批范围：**{data['range_start']}—{data['range_end']}**，对话截止 **{data['cutoff']}（北京时间）**。12 条对话、150 条用户消息，整理为 9 个有记录日期和 6 个留白日期。", "", "## 按日期查看", "", bullet([f"[{day['date']}](daily/{day['date']}.md) · {day['title']}" for day in reversed(calendar)]), "", "## 当前需要记住的边界", "", bullet(["插头抓取：第一次未拿起，第二次拿起并放回；两轮 73 次运行、474 张照片。", "快速抓取：代码和离线验证通过；快照末尾刚进入另一轮预检，尚无新流程完整实物结果。", "自主移动：有低速短移和保护停车记录；可靠导航、定位与单目避障实物验收未完成。"]), "", "## 资料怎么用", "", "先读某天摘要和结果，再看问题、后续与经验。需要核对时使用来源编号回到对应会话；原始 JSONL、工具记录和照片保存在私人快照，整理稿不代替原文。", "", "`index.html` 是自带全部整理数据的单文件，可直接离线打开、搜索、切换日期和打印；分享这个文件即可阅读全部日报与经验。Markdown 和 data.json 便于继续修订。", "", "## 后续追加", "", "收到新的归档请求后重新列出两个工作区的会话并记录新的截止时间，使用 [归档工具](../../scripts/archive_conversations.py) 建立新快照，再复核台账和差异更新 data.json。不要覆盖旧原始快照，不要把无记录日期写成“没有工作”。", "", "修改整理稿后运行：", "", "```powershell", "python scripts/build_journal.py", "python scripts/build_journal.py --check", "```", "", "私人 ZIP 可独立复核：", "", "```powershell", "python scripts/archive_conversations.py verify 'E:/ColdArchive/2026-11-03/XLeRobot-conversations-20261103.zip'", "```", "", "后续自动定时归档尚未配置。", ""]
    output["README.md"] = "\n".join(readme)
    data["calendar"] = calendar
    template = (ROOT / "viewer-template.html").read_text(encoding="utf-8")
    assert template.count("__JOURNAL_DATA__") == 1
    output["index.html"] = template.replace("__JOURNAL_DATA__", json.dumps(data, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c"))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    output = build()
    differences = []
    for relative, content in output.items():
        path = ROOT / relative
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                differences.append(relative)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
    if differences:
        raise SystemExit("Stale journal files: " + ", ".join(differences))
    print(f"Journal {'checked' if args.check else 'built'}: {len(output)} files, 15 dates, 12 sources")


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
