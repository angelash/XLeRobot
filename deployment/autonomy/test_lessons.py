import pytest

from deployment.autonomy import lessons

SAMPLE = '''# 助手自主经验（待审核）

## 怎么写

- 新经验追加在末尾。

## 条目

### L-001 动作中抓图的节奏
- 日期 2026-11-02；状态：待审核；把握：实测（`20261102-102644`）
- 机器人相机每张约 2.6 秒，判断碰撞仍然靠遥测。

### L-032 收尾轮位变化后原地图定位必须失效
- 日期 2026-11-03；状态：待审核
- 轮位变化后地图标为历史观察。

### L-032 语音响应分开计量
- 日期 2026-11-03；状态：待审核
- 轮次完成与实际发声分别记录。

### L-20261118-UNCOMMANDED-REFERENCE 未下令轮位变化与采集恢复分别留证
- 日期2026-11-18；状态待审核；依据持续任务。
- 续轮不清除原`uncommanded_pose_change`锁，右肩hold保持。
'''


def test_parse_reads_id_title_date_status_and_line_of_every_entry():
    entries, stray = lessons.parse(SAMPLE)
    assert not stray
    assert [(e['id'], e['date'], e['status']) for e in entries] == [
        ('L-001', '2026-11-02', '待审核'), ('L-032', '2026-11-03', '待审核'), ('L-032', '2026-11-03', '待审核'),
        ('L-20261118-UNCOMMANDED-REFERENCE', '2026-11-18', '待审核')]
    assert entries[0]['title'] == '动作中抓图的节奏' and entries[0]['line'] == 9
    assert '新经验追加在末尾' not in entries[0]['text'] and '遥测' in entries[0]['text']


def test_check_keeps_old_reused_numbers_as_notes_and_rejects_reused_dated_ids():
    entries, stray = lessons.parse(SAMPLE)
    result = lessons.check(entries, stray)
    assert result['ok'] and not result['problems']
    assert len(result['notes']) == 1 and 'L-032' in result['notes'][0] and '13、17' in result['notes'][0]
    again = SAMPLE + '\n### L-20261118-UNCOMMANDED-REFERENCE 另一条\n- 日期2026-11-19；状态待审核\n'
    result = lessons.check(*lessons.parse(again))
    assert not result['ok'] and 'L-20261118-UNCOMMANDED-REFERENCE 重复' in result['problems'][0]


def test_check_reports_a_dated_entry_without_date_or_status_and_a_stray_heading():
    text = SAMPLE + '\n### L-20261120-NO-HEADER 只有正文\n- 没有写首行。\n\n### 临时标题\n- 不是经验。\n'
    result = lessons.check(*lessons.parse(text))
    assert not result['ok'] and len(result['problems']) == 3
    assert any('缺少日期' in p for p in result['problems']) and any('缺少状态' in p for p in result['problems'])
    assert any('临时标题' in p for p in result['problems'])


def test_search_needs_every_term_unless_any_is_asked_and_ranks_titles_first():
    entries, _ = lessons.parse(SAMPLE)
    found = lessons.search(entries, '轮位 hold')
    assert [r['id'] for r in found] == ['L-20261118-UNCOMMANDED-REFERENCE'] and found[0]['matched'] == ['轮位', 'hold']
    assert 'uncommanded_pose_change' in found[0]['excerpt']
    either = lessons.search(entries, '轮位 hold', any_term=True)
    assert [r['line'] for r in either] == [21, 13]
    assert lessons.search(entries, 'UNCOMMANDED')[0]['line'] == 21, 'IDs and Latin terms ignore case'
    assert lessons.search(entries, '不存在的词') == []


def test_search_without_terms_lists_newest_and_honours_since_and_limit():
    entries, _ = lessons.parse(SAMPLE)
    assert [r['line'] for r in lessons.search(entries, '', limit=2)] == [21, 17]
    assert [r['id'] for r in lessons.search(entries, '', since='2026-11-10')] == ['L-20261118-UNCOMMANDED-REFERENCE']
    assert len(lessons.search(entries, '', limit=0)) == 1


def test_repository_lessons_file_follows_its_own_writing_rules():
    entries, stray = lessons.load()
    result = lessons.check(entries, stray)
    assert len(entries) >= 222 and result['ok'], result['problems']


def test_command_line_reports_problems_with_a_failing_exit_code(tmp_path, capsys):
    good, bad = tmp_path/'good.md', tmp_path/'bad.md'
    good.write_text(SAMPLE, encoding='utf-8')
    bad.write_text(SAMPLE + '\n### L-20261120-NO-HEADER 只有正文\n- 没有写首行。\n', encoding='utf-8')
    assert lessons.main(['check', '--path', str(good)]) == 0
    assert lessons.main(['check', '--path', str(bad)]) == 1
    assert 'L-20261120-NO-HEADER' in capsys.readouterr().out
    assert lessons.main(['search', '轮位', 'hold', '--path', str(good)]) == 0
    assert 'L-20261118-UNCOMMANDED-REFERENCE' in capsys.readouterr().out


def test_http_search_is_read_only_and_reports_bad_input(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from deployment.autonomy.integration import install
    source = tmp_path/'lessons.md'
    source.write_text(SAMPLE, encoding='utf-8')
    before = source.read_bytes()
    app = FastAPI()
    install(app, data_dir=tmp_path/'data', start=False, lessons_path=source)
    client = TestClient(app)
    r = client.get('/api/platform/autonomy/lessons', params={'q': '轮位 hold'})
    assert r.status_code == 200 and r.json()['entries'] == 4 and r.json()['check']['ok']
    assert [row['id'] for row in r.json()['results']] == ['L-20261118-UNCOMMANDED-REFERENCE']
    assert client.get('/api/platform/autonomy/lessons', params={'since': 'yesterday'}).status_code == 422
    assert client.post('/api/platform/autonomy/lessons', json={}).status_code == 405
    assert source.read_bytes() == before
    source.unlink()
    assert client.get('/api/platform/autonomy/lessons').status_code == 503
