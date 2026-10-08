"""Re-run a recorded test from results/runs.jsonl and compare the verdicts.

Usage: python3 replay.py <label> <new-label>

Only the record is used: the settings come from the "config" field (what the
containers actually ran with) when it exists, and from "args" otherwise.
"""
import json
import subprocess
import sys

label, new_label = sys.argv[1:3]
rec = [json.loads(l) for l in open('results/runs.jsonl') if json.loads(l)['label'] == label][-1]

args = [a for a in rec['args'].split() if not a.startswith(('API_', 'BACKEND_', 'INSTRUMENT_', 'TELEMETRY_', 'K6_CPUS='))]
if 'config' in rec:
    for svc in ('api', 'backend'):
        args += [f'{k}={v}' for k, v in rec['config'][svc]['env'].items() if not k.startswith('OTEL_')]
    args.append(f"K6_CPUS={rec['config']['k6_cpus']:g}")
    source = 'config'
else:
    args += [a for a in rec['args'].split() if a.startswith(('API_', 'BACKEND_', 'INSTRUMENT_', 'TELEMETRY_', 'K6_CPUS='))]
    source = 'args'

print(f'replaying {label} from {source}: {" ".join(args)}', file=sys.stderr)
out = subprocess.run(['./run.sh', new_label, *args], capture_output=True, text=True, check=True).stdout
new = json.loads(out.strip().splitlines()[-1])


def pick(r):
    return {
        'rate': round(r['reqs']['rate'], 1),
        'p95_ms': round(r['p95_ms'], 1),
        'failed': round(r['failed_rate'], 4),
        'dropped': (r.get('dropped') or {}).get('count', 0),
        'exit': r['k6_exit'],
    }


print(json.dumps({'source': source, 'recorded': pick(rec), 'replayed': pick(new),
                  'same_verdict': rec['k6_exit'] == new['k6_exit']}, ensure_ascii=False))
