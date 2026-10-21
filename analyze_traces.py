"""Summarize per-request time breakdown from Tempo for one run in runs.jsonl.

Usage: python3 analyze_traces.py [--same-set] <label> [limit]

The traces are taken evenly from 30 equal slices of the test's time range.

Each row is computed per trace first (for example, server span minus client
span of the same request) and then summarized as a distribution.

By default every row uses every trace that has the spans the row needs.
Requests that inventory rejected with 503 have no "lookup stock" span, so the
inventory rows then cover only accepted requests while the catalog-api rows
cover all of them. --same-set restricts every row to the traces that have all
of catalog-api's server and client spans and inventory's server and lookup
spans, so that the rows describe the same requests. The number of traces left
out is printed.
"""
import collections, datetime, json, statistics, sys, urllib.parse, urllib.request

TEMPO = 'http://localhost:13200'
argv = sys.argv[1:]
same_set = '--same-set' in argv
argv = [a for a in argv if a != '--same-set']
label = argv[0]
limit = int(argv[1]) if len(argv) > 1 else 300
run = [json.loads(l) for l in open('results/runs.jsonl') if json.loads(l)['label'] == label][-1]
ts = lambda s: int(datetime.datetime.fromisoformat(s.replace('Z', '+00:00')).timestamp())
# Tempo returns search results in no particular time order, so a single search
# with a limit can over-sample one part of the test (for example the start,
# before the queue has built up). Split the test into equal slices and take the
# same number of traces from each.
SLICES = 30
start, end = ts(run['start']), ts(run['end']) + 1
per_slice = []
for i in range(SLICES):
    s0 = start + (end - start) * i // SLICES
    s1 = start + (end - start) * (i + 1) // SLICES
    q = urllib.parse.urlencode({'q': '{resource.service.name="catalog-api" && kind=server}',
                                'start': s0, 'end': s1, 'limit': 5 * -(-limit // SLICES), 'spss': 1})
    found = [t for t in json.load(urllib.request.urlopen(f'{TEMPO}/api/search?{q}'))['traces']
             if s0 * 1e9 <= int(t['startTimeUnixNano']) < s1 * 1e9]
    per_slice.append(sorted(found, key=lambda t: t['startTimeUnixNano']))
# Take one trace from each slice in turn until the limit is reached, so that
# slices with few requests (the end of the test) do not shrink the sample.
traces, seen, k = [], set(), 0
while len(traces) < limit and any(len(f) > k for f in per_slice):
    for f in per_slice:
        if len(f) > k and len(traces) < limit and f[k]['traceID'] not in seen:
            seen.add(f[k]['traceID']); traces.append(f[k])
    k += 1
expected = min(limit, run.get('iterations', {}).get('count', limit))
if len(traces) < expected:
    print(f'warning: Tempo returned {len(traces)} traces, expected {expected}. '
          'Ingestion may not be finished; results from a partial sample can be biased.', file=sys.stderr)

rows = collections.defaultdict(list)
excluded = 0
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
    if same_set and None in (server, client, bserver, lookup):
        excluded += 1
        continue
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

print(f'{label}: {len(traces)} traces' + (f', {excluded} excluded by --same-set' if same_set else ''))
for k, v in rows.items():
    v = sorted(x for x in v if x is not None)
    if v:
        print(f'  {k:44s} n={len(v):4d} med={statistics.median(v):7.1f}ms p95={v[int(len(v)*0.95)-1]:7.1f}ms')
