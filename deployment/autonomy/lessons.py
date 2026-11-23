"""Index and search of the assistant's pending lessons (AGENT_LEARNING_ZH.md).

The Markdown file stays the record; this module only reads it. It answers
"which lessons mention this joint, sensor or failure" without rereading the
whole file, and checks new entries against the file's own writing rules.
A few old numeric IDs are reused by two entries; they keep their IDs, so every
result also carries its line number.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SOURCE = Path(__file__).resolve().parents[2]/'fabrication'/'assembly'/'debug_console'/'AGENT_LEARNING_ZH.md'
HEADER = re.compile(r'^### (L-[0-9A-Za-z_-]+)\s+(.+?)\s*$')
DATED_ID = re.compile(r'^L-(\d{4})(\d{2})(\d{2})-.')
DATE = re.compile(r'日期[：:\s]*(\d{4}-\d{2}-\d{2})')
STATUS = re.compile(r'状态[：:\s]*([^；;，,。\s]+)')


def parse(text):
    """Entries in file order, plus level-3 headings that are not lesson IDs."""
    entries, stray, current = [], [], None
    for number, line in enumerate(text.splitlines(), 1):
        match = HEADER.match(line)
        if match:
            current = {'id': match.group(1), 'title': match.group(2), 'line': number, 'lines': []}
            entries.append(current)
        elif line.startswith('### '):
            stray.append({'line': number, 'heading': line[4:].strip()})
        elif line.startswith(('# ', '## ')):
            current = None
        elif current is not None:
            current['lines'].append(line)
    for entry in entries:
        entry['text'] = '\n'.join(entry.pop('lines')).strip()
        first = entry['text'].split('\n', 1)[0]
        date, status = DATE.search(first), STATUS.search(first)
        entry['date'] = date.group(1) if date else None
        entry['status'] = status.group(1) if status else None
    return entries, stray


def load(path=None):
    return parse(Path(path or SOURCE).read_text(encoding='utf-8'))


def check(entries, stray=()):
    """Problems break the writing rules for dated entries; notes are kept history."""
    problems, notes = [], []
    by_id = {}
    for entry in entries:
        by_id.setdefault(entry['id'], []).append(entry)
    for key, rows in by_id.items():
        if len(rows) > 1:
            where = '、'.join(str(r['line']) for r in rows)
            if DATED_ID.match(key):
                problems.append(f'{key} 重复，行 {where}')
            else:
                notes.append(f'旧编号 {key} 有 {len(rows)} 条（行 {where}），引用时带日期和标题')
    for entry in entries:
        if not DATED_ID.match(entry['id']):
            continue
        for field, label in (('date', '日期'), ('status', '状态')):
            if not entry[field]:
                problems.append(f'{entry["id"]}（行 {entry["line"]}）首行缺少{label}')
    for row in stray:
        problems.append(f'行 {row["line"]} 的三级标题不是经验编号：{row["heading"][:40]}')
    return {'ok': not problems, 'problems': problems, 'notes': notes}


def stats(entries):
    by_status, by_month = {}, {}
    for entry in entries:
        by_status[entry['status'] or '未写'] = by_status.get(entry['status'] or '未写', 0) + 1
        month = entry['date'][:7] if entry['date'] else '未写'
        by_month[month] = by_month.get(month, 0) + 1
    return {'entries': len(entries), 'by_status': by_status, 'by_month': dict(sorted(by_month.items())),
            'newest': [brief(e) for e in entries[-3:][::-1]]}


def brief(entry, terms=()):
    lines = [line for line in entry['text'].split('\n') if line.strip()]
    chosen = next((line for line in lines[1:] if any(t in line.casefold() for t in terms)), None)
    chosen = chosen or (lines[1] if len(lines) > 1 else (lines[0] if lines else ''))
    return {'id': entry['id'], 'title': entry['title'], 'date': entry['date'], 'status': entry['status'],
            'line': entry['line'], 'excerpt': chosen.lstrip('- ').strip()[:200]}


def search(entries, query='', *, limit=8, any_term=False, since=None):
    """Entries containing every term (or any, when asked), best match first, then newest.

    Terms are plain substrings separated by spaces, so Chinese needs no word
    splitting. An empty query lists the newest entries.
    """
    terms = [t.casefold() for t in str(query or '').split()]
    rows = []
    for entry in entries:
        if since and (entry['date'] or '') < since:
            continue
        heading, text = (entry['id']+' '+entry['title']).casefold(), entry['text'].casefold()
        weight = {t: heading.count(t)*5 + min(text.count(t), 5) for t in terms}
        matched = [t for t in terms if weight[t]]
        if terms and (not matched or (not any_term and len(matched) < len(terms))):
            continue
        rows.append((len(matched), sum(weight.values()), entry['line'], entry, matched))
    rows.sort(key=lambda row: row[:3], reverse=True)
    limit = max(1, min(int(limit), 50))
    return [{**brief(entry, matched), 'matched': matched} for *_, entry, matched in rows[:limit]]


def report(query='', *, limit=8, any_term=False, since=None, path=None):
    if since and not re.fullmatch(r'\d{4}-\d{2}-\d{2}', str(since)):
        raise ValueError('since 需为 YYYY-MM-DD')
    entries, stray = load(path)
    return {'source': Path(path or SOURCE).name, 'query': query, **stats(entries), 'check': check(entries, stray),
            'results': search(entries, query, limit=limit, any_term=any_term, since=since)}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command', choices=('search', 'check', 'stats'))
    parser.add_argument('terms', nargs='*')
    parser.add_argument('--path', type=Path, default=SOURCE)
    parser.add_argument('--limit', type=int, default=8)
    parser.add_argument('--any', action='store_true', help='match any term instead of all')
    parser.add_argument('--since', help='YYYY-MM-DD')
    parser.add_argument('--full', action='store_true', help='print whole entries')
    parser.add_argument('--json', action='store_true')
    args = parser.parse_args(argv)
    # Piped Windows output uses the ANSI code page, which lacks some characters used in the entries.
    sys.stdout.reconfigure(errors='backslashreplace')
    entries, stray = load(args.path)
    if args.command == 'check':
        result = check(entries, stray)
        print(json.dumps(result, ensure_ascii=False, indent=2) if args.json else
              '\n'.join(['问题：'+p for p in result['problems']] + ['备注：'+n for n in result['notes']] or ['没有问题']))
        return 0 if result['ok'] else 1
    if args.command == 'stats':
        print(json.dumps(stats(entries), ensure_ascii=False, indent=2))
        return 0
    found = search(entries, ' '.join(args.terms), limit=args.limit, any_term=args.any, since=args.since)
    if args.json:
        print(json.dumps(found, ensure_ascii=False, indent=2))
        return 0
    whole = {e['line']: e['text'] for e in entries} if args.full else {}
    for row in found:
        print(f'{row["id"]} {row["title"]}（{row["date"] or "无日期"}，{row["status"] or "无状态"}，行 {row["line"]}）')
        print(whole[row['line']] if args.full else '  '+row['excerpt'])
    if not found:
        print('没有匹配的经验；可减少关键词或加 --any')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
