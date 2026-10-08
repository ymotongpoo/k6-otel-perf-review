"""Summarize per-request time breakdown from Tempo for one run in runs.jsonl.

Usage: python3 analyze_traces.py <label> [limit]
"""
import collections, datetime, json, statistics, sys, urllib.parse, urllib.request

TEMPO = 'http://localhost:13200'
label = sys.argv[1]
limit = int(sys.argv[2]) if len(sys.argv) > 2 else 300
run = [json.loads(l) for l in open('results/runs.jsonl') if json.loads(l)['label'] == label][-1]
ts = lambda s: int(datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp())
q = urllib.parse.urlencode({'q': '{resource.service.name="catalog-api" && kind=server}',
                            'start': ts(run['start']), 'end': ts(run['end']) + 5, 'limit': limit, 'spss': 1})
traces = json.load(urllib.request.urlopen(f'{TEMPO}/api/search?{q}'))['traces']
expected = min(limit, run.get('iterations', {}).get('count', limit))
if len(traces) < expected:
    print(f'warning: Tempo returned {len(traces)} traces, expected {expected}. '
          'Ingestion may not be finished; results from a partial sample can be biased.', file=sys.stderr)

rows = collections.defaultdict(list)
for t in traces:
    body = json.load(urllib.request.urlopen(f"{TEMPO}/api/v2/traces/{t['traceID']}"))
    d = {}
    for rs in body['trace']['resourceSpans']:
        svc = next(a['value']['stringValue'] for a in rs['resource']['attributes'] if a['key'] == 'service.name')
        for ss in rs['scopeSpans']:
            for sp in ss['spans']:
                d[(svc, sp['name'])] = (int(sp['endTimeUnixNano']) - int(sp['startTimeUnixNano'])) / 1e6
    server = d.get(('catalog-api', 'GET /items/{id}'))
    client = d.get(('catalog-api', 'HTTP GET'))
    wait = d.get(('catalog-api', 'wait for backend slot'))
    bserver = d.get(('inventory', 'GET /stock/{id}'))
    lookup = d.get(('inventory', 'lookup stock'))
    rows['api server span'].append(server)
    if client is not None:
        rows['api client span'].append(client)
        rows['server - client (inferred api-side)'].append(server - client)
    if wait is not None:
        rows['wait span (explicit)'].append(wait)
    if bserver is not None and lookup is not None:
        rows['inventory server span'].append(bserver)
        rows['inventory lookup span'].append(lookup)
        rows['inventory server - lookup (inferred queue)'].append(bserver - lookup)

print(f'{label}: {len(traces)} traces')
for k, v in rows.items():
    v = sorted(x for x in v if x is not None)
    if v:
        print(f'  {k:44s} n={len(v):4d} med={statistics.median(v):7.1f}ms p95={v[int(len(v)*0.95)-1]:7.1f}ms')
