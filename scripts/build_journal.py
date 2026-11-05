"""Build the reviewed public journal without accessing chats or robot hardware."""
from __future__ import annotations
from copy import deepcopy
from datetime import date, timedelta
import argparse
import json
from pathlib import Path
import subprocess
import sys

REPO = Path(__file__).resolve().parents[1]
ROOT = REPO / "docs/journal"


def bullets(items):
    return "\n".join("- " + item for item in items)


def source_links(ids, parent=""):
    return "、".join(f"[{sid}]({parent}SOURCES.md#{sid.lower()})" for sid in ids)


def prepare(source):
    data = deepcopy(source)
    stats = data["snapshot"]
    ids = {s["id"] for s in data["sources"]}
    assert len(ids) == len(data["sources"]) == stats["threads"]
    assert len({s["thread"] for s in data["sources"]}) == stats["threads"]
    known = {d["date"]: d for d in data["days"]}
    assert len(known) == len(data["days"])
    for item in [*data["days"], *data["lessons"]]:
        assert set(item["sources"]).issubset(ids)
    calendar = []
    current, end = date.fromisoformat(data["range_start"]), date.fromisoformat(data["range_end"])
    assert current <= end
    while current <= end:
        day = current.isoformat()
        calendar.append(known.get(day, {"date": day, "title": "未找到当日记录", "status": "记录空白", "summary": "归档来源中未找到这一天的对话或台账条目；这不代表当天没有活动。", "sources": [], "progress": [], "problems": [], "next": [], "lessons": [], "commits": [], "evidence": [], "empty": True}))
        current += timedelta(days=1)
    assert set(known).issubset({d["date"] for d in calendar})
    data["calendar"] = calendar
    data["display"] = {"calendar_days":len(calendar), "recorded_days":len(known), "blank_days":len(calendar)-len(known), "lessons":len(data["lessons"])}
    return data


def daily(day, data):
    cutoff = day.get("cutoff", data["cutoff"])
    lines = [f"# {day['date']} · {day['title']}", "", f"阶段：{day['status']}。时间口径：北京时间；本日整理依据截止 {cutoff}。", "", day["summary"], ""]
    if day.get("empty"):
        lines += ["请保留这个空白，后续找到原始资料后再追加。不要把前后日期的工作倒填成当日事实。", ""]
    else:
        for heading, key in [("当天推进", "progress"), ("问题与失败", "problems")]:
            lines += ["## " + heading, "", bullets(day[key]), ""]
        lines += ["## 当日结果与边界", "", day["outcome"], "", "## 后续事项", "", bullets(day["next"]), "", "## 可复用经验", "", bullets(day["lessons"]), "", "## 来源与证据", "", "会话：" + source_links(day["sources"], "../") + "。", "", bullets([f"[{p}](../../../{p})" for p in day["evidence"]]), ""]
        if day["commits"]:
            lines += ["关键历史提交：" + "、".join(f"[{c}](https://github.com/angelash/XLeRobot/commit/{c})" for c in day["commits"]) + "。", ""]
        lines += ["以上是历史复盘。设计、软件验证、发布与实物结果分别表述；待办不是新的动作授权。", ""]
    lines += ["[返回日期索引](../README.md) · [经验整理](../EXPERIENCE_ZH.md) · [覆盖范围](../COVERAGE.md)", ""]
    return "\n".join(lines)


def render_sources(data):
    stats, inventory = data["snapshot"], data["inventory"]
    lines = ["# 会话来源目录", "", f"应用当前可见：XLeRobot {inventory['XLeRobot']} 条全部纳入；open-share {inventory['open-share']} 条中纳入关联的 {stats['related_threads']} 条。对话截取至 {data['cutoff']}（北京时间）。", "", "来源编号持续使用；thread ID 是稳定回查标识。按每条消息的 UTC 时间转换到北京时间，不按会话创建日归档。", ""]
    for source in data["sources"]:
        lines += [f'<a id="{source["id"].lower()}"></a>', "", f"## {source['id']} · {source['title']}", "", f"- 工作区：{source['workspace']}", f"- 会话：`{source['thread']}`", "- 纳入原因：" + source["reason"], f"- 本批私人阅读稿：`{stats['private_snapshot']}/transcripts/{source['thread']}.md`（仅本机备份内）。"]
        if "parent" in source:
            lines += [f"- 分叉来源：{source['parent']}；继承前缀 {source['inherited_records']} 条记录 / {source['inherited_bytes']:,} 字节。保留父文件原字节，阅读时恢复继承；日报只计自身新增消息。"]
        lines.append("")
    lines += ["## 辅助材料", "", "用 PROJECT_STATE.md、PROJECT_HANDOFF_ZH.md、观察手册、待审核经验、Git 历史与已有本地证据交叉核对。历史实物测试与系统核验沿用对应时点证据，不代表本次归档重新操作机器人。", "", "## open-share 未纳入的当前会话", "", bullets(data["inventory"]["excluded_open_share"]), "", "当前其余会话与机器人主线无直接关系；旧历史失效路径和全部检索边界见 [覆盖说明](COVERAGE.md)。", ""]
    return "\n".join(lines)


def render_lessons(data):
    lines = ["# 从 XLeRobot 制作过程整理出的经验", "", f"范围：{data['range_start']} 至 {data['range_end']}；对话截止 {data['cutoff']}（北京时间）。这是分享复盘稿，不替代当前机器人控制规范。", ""]
    for n, note in enumerate(data["lessons"], 1):
        lines += [f"## {n:02d} · {note['title']}", "", "发生日期：" + "、".join(note["dates"]) + "。", "", "遇到的问题：" + note["problem"], "", "后来采用的做法：" + note["method"], "", "适用边界：" + note["limit"], "", "来源：" + source_links(note["sources"]) + "。", ""]
    lines += ["## 可展开的分享叙事", "", bullets(data["story_outline"]), "", "## 分享前保留的事实边界", "", bullets(data["sharing_boundaries"]), ""]
    return "\n".join(lines)


def render_coverage(data):
    s, d = data["snapshot"], data["display"]
    lines = ["# 本批归档的覆盖与备份边界", "", f"对话采样截止：{data['cutoff']}（北京时间）；原始 UTC 截止为 `{data['cutoff_utc']}`。支持材料采集结束 UTC：`{s['support_finished_utc']}`。文件逐一稳定读取，非跨文件原子快照。", "", "## 已保留", "", bullets([f"{s['threads']} 条会话：XLeRobot 全部 {s['project_threads']} 条、open-share 关联 {s['related_threads']} 条；{s['turns']} 个自身回合，{s['messages']:,} 条去重用户/助手消息，其中用户 {s['user_messages']} 条。", f"较前批新增 {s['delta_messages']} 条用户/助手消息、{s['delta_user_messages']} 条用户消息、{s['delta_turns']} 个回合，来自 {s['changed_threads']} 条会话；前次12份原始文件前缀全部保持一致。", f"{s['raw_variants']} 份原始JSONL，{s['raw_bytes']:,} 字节，保留工具记录、行内图像与分叉元数据；{s['forks']} 条分叉核对字节/记录边界，日报不重复计入继承。", f"{s['attachment_references']} 个用户图片引用：{s['copied_attachments']} 个原文件、{s['recovered_inline_attachments']} 个会话内嵌图版本。内嵌版本可解码，但不能声称恢复原文件字节。", f"补存 {s['support_files']:,} 个已有本地证据文件，{s['support_bytes']:,} 字节，其中 {s['support_images']:,} 个图片文件。依据源修改时间跳过 {s['support_skipped']} 个晚于截止或读取期间变化的文件。首批抓取474张照片继续保留，新增证据按场次目录归档。", f"{d['calendar_days']} 个连续日期，{d['recorded_days']} 天有记录、{d['blank_days']} 天明确留白；{d['lessons']} 条可复用经验。"]), "", "## 未宣称覆盖的内容", "", bullets(data["coverage_notes"]), "", "## 存储与验证", "", f"私人快照：`{s['private_snapshot']}`，Git忽略。", "", f"E盘：`{s['backup']}`。", "", f"G盘：`{s['secondary_backup']}`。", "", "本机核验E/F同为物理磁盘1，G为物理磁盘2。原始备份均在本机，未上传网盘。ZIP封存后逐文件读回核对路径、长度和SHA-256，回执在ZIP外生成，见 [本批回执](BACKUP_RECEIPT.md)。", "", "## 历史批次", "", "首批原始快照、E/G备份与回执保持原样。本批是新的完整快照，另有与前批的新增消息及字节前缀对比。历次截止、文件数量与哈希见 [归档批次](ARCHIVE_HISTORY.md)。", ""]
    return "\n".join(lines)


def render_history(data):
    lines = ["# 归档批次与更新记录", "", "旧原始快照和ZIP不覆盖；公开日志随新证据累计更新。每批有独立截止和验证回执。", ""]
    for batch in data["batches"]:
        lines += ["## " + batch["id"], "", f"- 对话截止：{batch['cutoff']}（北京时间）。", f"- {batch['threads']} 条会话、{batch['messages']:,} 条用户/助手消息，其中用户 {batch['user_messages']} 条；{batch['turns']} 个自身回合。", f"- {batch['calendar_days']} 个日期，{batch['recorded_days']} 天有记录。", f"- E盘：`{batch['primary']}`。", f"- G盘：`{batch['secondary']}`。", f"- [验证回执]({batch['receipt']})。"]
        if "sha256" in batch:
            lines += [f"- ZIP：{batch['bytes']:,} 字节，{batch['files']} 个有效载荷文件；SHA-256 `{batch['sha256']}`。"]
        lines.append("")
    s = data["snapshot"]
    lines += ["## 本次增加了什么", "", bullets([f"新增 {s['delta_user_messages']} 条用户消息和 {s['delta_messages']} 条用户/助手消息；两条会话有变化。", "11月3日补齐快速抓取部署、工作站本地识别接班和移动方向纠正，保留此前仅完成离线验证的历史阶段。", "新增11月4日分组抓放、衬垫变化、固定批次失败和感知反例。", "新增11月5日局部本地补偿、模型候选与仍在进行的午前训练。", "离线页统计、截止时间与经验数量改为从数据生成，继续追加时不再沿用首批数字。"]), ""]
    return "\n".join(lines)


def render_readme(data):
    s, d = data["snapshot"], data["display"]
    lines = ["# XLeRobot 每日制作日志", "", "每天回看进展，也为后续经验分享保留可回查的依据。", "", "[打开离线阅读页](index.html) · [经验分享稿](EXPERIENCE_ZH.md) · [会话来源](SOURCES.md) · [覆盖说明](COVERAGE.md) · [本批备份回执](BACKUP_RECEIPT.md) · [归档批次](ARCHIVE_HISTORY.md)", "", f"范围：**{data['range_start']}—{data['range_end']}**；对话截止 **{data['cutoff']}（北京时间）**。{s['threads']}条对话、{s['user_messages']}条用户消息，{d['recorded_days']}个有记录日期、{d['blank_days']}个留白日期。", "", f"本次增加 {s['delta_user_messages']} 条用户消息，补11月3日下午并增加11月4日、11月5日上午；11月5日午前训练尚未收尾。", "", "## 按日期查看", "", bullets([f"[{day['date']}](daily/{day['date']}.md) · {day['title']}" for day in reversed(data["calendar"])]), "", "## 当前需要记住的边界", "", bullets(data["current_boundaries"]), "", "## 资料怎么用", "", "先看当天摘要与结果，再读问题、后续和经验。需要核对时用来源编号回查会话；原始JSONL、工具记录、照片与逐文件清单仅在私人快照。", "", "`index.html`自带全部整理数据，可直接离线打开、检索、切换日期、查看经验与来源、打印。单独分享该文件即可阅读；Markdown和data.json便于修订。", "", "## 后续追加", "", "重新列出两个工作区会话并建立新截止点，使用 [归档工具](../../scripts/archive_conversations.py)创建新快照，核对台账和新增证据再更新data.json。旧原始快照和ZIP不覆盖；活跃任务截止后的进展留待下一批。", "", "```powershell", "python scripts/build_journal.py", "python scripts/build_journal.py --check", f"python scripts/archive_conversations.py verify '{s['backup']}'", "```", "", "自动定时归档尚未配置。", ""]
    return "\n".join(lines)


def build():
    data = prepare(json.loads((ROOT / "data.json").read_text(encoding="utf-8")))
    for day in data["days"]:
        for evidence in day["evidence"]:
            if not (REPO/evidence).exists():
                raise ValueError("Missing evidence path: " + evidence)
        for commit in day["commits"]:
            subprocess.run(["git", "rev-parse", "--verify", commit+"^{commit}"],cwd=REPO,check=True,capture_output=True)
    output = {f"daily/{day['date']}.md":daily(day,data) for day in data["calendar"]}
    output.update({"README.md":render_readme(data),"SOURCES.md":render_sources(data),"EXPERIENCE_ZH.md":render_lessons(data),"COVERAGE.md":render_coverage(data),"ARCHIVE_HISTORY.md":render_history(data)})
    template = (ROOT/"viewer-template.html").read_text(encoding="utf-8")
    assert template.count("__JOURNAL_DATA__") == 1
    output["index.html"] = template.replace("__JOURNAL_DATA__",json.dumps(data,ensure_ascii=False,separators=(",",":")).replace("<","\\u003c"))
    return output


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check",action="store_true")
    args=parser.parse_args()
    output=build()
    stale=[]
    for relative,content in output.items():
        path=ROOT/relative
        if args.check:
            if not path.exists() or path.read_text(encoding="utf-8")!=content:stale.append(relative)
        else:
            path.parent.mkdir(parents=True,exist_ok=True)
            path.write_text(content,encoding="utf-8")
    if stale:raise SystemExit("Stale journal files: "+", ".join(stale))
    print(f"Journal {'checked' if args.check else 'built'}: {len(output)} files")


if __name__=="__main__":
    if hasattr(sys.stdout,"reconfigure"):sys.stdout.reconfigure(encoding="utf-8")
    main()
