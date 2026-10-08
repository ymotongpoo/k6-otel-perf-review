"""Re-run a recorded test from results/runs.jsonl and compare the verdicts.

Usage: python3 replay.py <label> <new-label>

Only the record is used to choose the settings. Load settings (MODEL, RATE,
DURATION, ...) come from "args". When the record has a "config" field, the
applications' environment variables and the CPU limits of api, backend and k6
come from it; otherwise they come from "args".

The code itself is not restored. After the run, the script hash and the
images are compared with the record, and any difference is reported: a
replay with different code is a different test.
"""
import hashlib
import json
import subprocess
import sys

APP_PREFIXES = ('API_', 'BACKEND_', 'INSTRUMENT_', 'TELEMETRY_', 'K6_CPUS=')

label, new_label = sys.argv[1:3]
rec = [json.loads(l) for l in open('results/runs.jsonl') if json.loads(l)['label'] == label][-1]

args = [a for a in rec['args'].split() if not a.startswith(APP_PREFIXES)]
if 'config' in rec:
    cfg = rec['config']
    for svc in ('api', 'backend'):
        args += [f'{k}={v}' for k, v in cfg[svc]['env'].items()
                 if not k.startswith('OTEL_') and k != 'BACKEND_URL']
    args += [f"API_CPUS={cfg['api']['cpus']:g}", f"BACKEND_CPUS={cfg['backend']['cpus']:g}",
             f"K6_CPUS={cfg['k6_cpus']:g}"]
    source = 'config'
else:
    args += [a for a in rec['args'].split() if a.startswith(APP_PREFIXES)]
    source = 'args'
args = list(dict.fromkeys(args))

if 'config' in rec:
    sha = hashlib.sha256(open('k6/test.js', 'rb').read()).hexdigest()
    if sha != rec['config']['script_sha256']:
        print(f'warning: k6/test.js differs from the recorded script ({sha[:12]} != '
              f"{rec['config']['script_sha256'][:12]})", file=sys.stderr)

print(f'replaying {label} from {source}: {" ".join(args)}', file=sys.stderr)
out = subprocess.run(['./run.sh', new_label, *args], capture_output=True, text=True, check=True).stdout
new = json.loads(out.strip().splitlines()[-1])

mismatches = []
if 'config' in rec:
    old_cfg, new_cfg = rec['config'], new['config']
    checks = {
        'script_sha256': (old_cfg.get('script_sha256'), new_cfg.get('script_sha256')),
        'api image': (old_cfg['api'].get('image'), new_cfg['api'].get('image')),
        'backend image': (old_cfg['backend'].get('image'), new_cfg['backend'].get('image')),
        'k6 image': (old_cfg.get('k6_image'), new_cfg.get('k6_image')),
        'api cpus': (old_cfg['api'].get('cpus'), new_cfg['api'].get('cpus')),
        'backend cpus': (old_cfg['backend'].get('cpus'), new_cfg['backend'].get('cpus')),
    }
    for name, (old, now) in checks.items():
        if old is None:
            mismatches.append(f'{name}: not recorded')
        elif old != now:
            mismatches.append(f'{name}: recorded {old}, replayed {now}')
    for m in mismatches:
        print(f'warning: {m}', file=sys.stderr)
else:
    mismatches.append('record has no config; code, images and CPU limits were not checked')


def pick(r):
    return {
        'rate': round(r['reqs']['rate'], 1),
        'p95_ms': round(r['p95_ms'], 1),
        'failed': round(r['failed_rate'], 4),
        'dropped': (r.get('dropped') or {}).get('count', 0),
        'exit': r['k6_exit'],
    }


print(json.dumps({'source': source, 'recorded': pick(rec), 'replayed': pick(new),
                  'same_verdict': rec['k6_exit'] == new['k6_exit'],
                  'mismatches': mismatches}, ensure_ascii=False))
