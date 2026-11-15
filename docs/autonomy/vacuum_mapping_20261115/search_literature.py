"""Bounded scholarly discovery via Crossref. Results are metadata, not papers."""
from datetime import datetime, timezone
import json
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import time


QUERIES = [
    'robot vacuum cleaner SLAM mapping navigation review',
    'occupancy grid autonomous frontier exploration mobile robots',
    'indoor complete coverage path planning mobile robot survey',
    'lidar visual SLAM dynamic indoor environment review',
    'robot vacuum cleaner multi floor map room segmentation',
]


def search(query):
    params = urlencode({'query.bibliographic': query, 'rows': 8,
                        'select': 'DOI,title,author,published,URL,type'})
    url = 'https://api.crossref.org/works?'+params
    result = {'query': query, 'url': url, 'retrieved_at': datetime.now(timezone.utc).isoformat(),
              'evidence_level': 'discovery_metadata_only_not_full_text', 'rows_requested': 8}
    try:
        request = Request(url, headers={'User-Agent': 'XLeRobot-public-research/1.0'})
        with urlopen(request, timeout=25) as response:
            raw = response.read(1000000)
        message = json.loads(raw)['message']
        result.update(status='retrieved', total_results=message.get('total-results'), items=message.get('items', []))
    except Exception as exc:
        result.update(status='unavailable', error=type(exc).__name__+': '+str(exc)[:120], items=[])
    return result


def main():
    path = Path(__file__).with_name('LITERATURE_DISCOVERY.json')
    old = {r['query']: r for r in json.loads(path.read_text(encoding='utf-8'))['queries']} if path.exists() else {}
    results = []
    for query in QUERIES:
        previous = old.get(query)
        if previous and previous['status'] == 'retrieved':
            results.append(previous)
            continue
        result = search(query)
        if previous:
            result['previous_attempts'] = previous.get('previous_attempts', []) + [{k: v for k, v in previous.items() if k != 'previous_attempts'}]
        results.append(result)
        time.sleep(1.1)  # Respect the observed shared provider rate limit.
    output = {'scope': 'Five bounded discovery queries, eight ranked metadata hits each; not an exhaustive or screened systematic review',
              'full_text_evidence': 'Use SOURCES.json and the annotated implementation decisions separately', 'queries': results}
    path.write_text(json.dumps(output, ensure_ascii=False, indent=2)+'\n', encoding='utf-8')
    for result in results:
        print(result['status'], result['query'])
        for item in result['items'][:4]:
            print(' ', item.get('DOI'), ' | '.join(item.get('title', [])))


if __name__ == '__main__':
    main()
