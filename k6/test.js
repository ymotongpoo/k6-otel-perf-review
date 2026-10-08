// One iteration sends one GET /items/{id} to catalog-api.
//
// MODEL=closed uses constant-vus: each VU waits for the response before it
// sends the next request. MODEL=open uses constant-arrival-rate: k6 starts
// RATE iterations per second regardless of how long each one takes.
import http from 'k6/http';

const model = __ENV.MODEL || 'closed';
const duration = __ENV.DURATION || '60s';
const label = __ENV.LABEL || model;

const scenarios = {
  closed: {
    executor: 'constant-vus',
    vus: Number(__ENV.VUS || 10),
    duration,
  },
  open: {
    executor: 'constant-arrival-rate',
    rate: Number(__ENV.RATE || 100),
    timeUnit: '1s',
    duration,
    preAllocatedVUs: Number(__ENV.PRE_VUS || 50),
    maxVUs: Number(__ENV.MAX_VUS || 200),
  },
};

const thresholds = {
  http_req_duration: [`p(95)<${__ENV.P95_MS || 200}`],
  http_req_failed: ['rate<0.01'],
};
// CHECK_DROPPED=true fails the test when any iteration could not start.
if (__ENV.CHECK_DROPPED === 'true') {
  thresholds.dropped_iterations = ['count==0'];
}

export const options = {
  scenarios: { [model]: scenarios[model] },
  thresholds,
  summaryTrendStats: ['avg', 'min', 'med', 'p(90)', 'p(95)', 'p(99)', 'max'],
  tags: { testid: label },
};

const base = __ENV.BASE_URL || 'http://api:8080';
const timeout = __ENV.TIMEOUT || '10s';

export default function () {
  const id = Math.floor(Math.random() * 1000);
  http.get(`${base}/items/${id}`, { timeout, tags: { name: 'GET /items/{id}' } });
}

export function handleSummary(data) {
  const out = {};
  out[`/results/${label}.json`] = JSON.stringify(data, null, 2);
  out.stdout = JSON.stringify({
    label,
    model,
    reqs: data.metrics.http_reqs?.values,
    iterations: data.metrics.iterations?.values,
    dropped: data.metrics.dropped_iterations?.values ?? null,
    p95_ms: data.metrics.http_req_duration?.values['p(95)'],
    med_ms: data.metrics.http_req_duration?.values.med,
    failed_rate: data.metrics.http_req_failed?.values.rate,
    vus_max: data.metrics.vus_max?.values,
    thresholds_ok: Object.values(data.metrics).every(m => !m.thresholds || Object.values(m.thresholds).every(t => t.ok)),
  }) + '\n';
  return out;
}
