import http from 'k6/http';
import { Counter } from 'k6/metrics';

const config = JSON.parse(__ENV.RAG_LOAD_CONFIG);
const { origin, articleId, runId, publicationId, versionId, rate, seconds } = config;
const failures = new Counter('read_failures');
const requests = new Counter('read_requests');
const routes = [
  { name: 'library', path: '/api/articles', valid: body => body.articles?.some(article => article.id === articleId) },
  { name: 'run', path: '/api/article-runs/' + encodeURIComponent(runId),
    valid: body => body.id === runId && body.publication?.versionId === versionId },
  { name: 'results', path: '/trpc/publications.review?input=' + encodeURIComponent(JSON.stringify({ publicationId, versionId })),
    valid: body => body.result?.data?.version?.versionId === versionId &&
      body.result?.data?.publication?.id === publicationId },
];

export const options = {
  scenarios: {
    read: { executor: 'constant-arrival-rate', rate, timeUnit: '1s', duration: seconds + 's',
      preAllocatedVUs: 16, maxVUs: 64, gracefulStop: '6s' },
  },
  summaryTrendStats: ['med', 'p(95)'],
  thresholds: {
    dropped_iterations: ['count==0'],
    ...Object.fromEntries(routes.flatMap(({ name }) => [
      [`http_req_duration{route:${name}}`, ['p(95)>=0']],
      [`read_requests{route:${name}}`, ['count>0']],
      [`read_failures{route:${name}}`, ['count==0']],
    ])),
  },
};

export default function () {
  for (const { name, path, valid } of routes) {
    const tags = { route: name, name };
    const response = http.get(origin + path, { tags, timeout: '5s', redirects: 0 });
    let correct = false;
    if (response.status === 200) {
      try { correct = !!valid(response.json()); } catch (_) { /* Count invalid JSON as an error. */ }
    }
    failures.add(correct ? 0 : 1, tags);
    requests.add(1, tags);
  }
}

export function handleSummary({ metrics, state }) {
  const rows = Object.fromEntries(routes.map(({ name }) => {
    const timings = metrics[`http_req_duration{route:${name}}`]?.values;
    const failed = metrics[`read_failures{route:${name}}`]?.values.count;
    const sent = metrics[`read_requests{route:${name}}`]?.values;
    return [name, { requests: sent?.count ?? 0, errors: failed ?? null,
      requests_per_second: sent?.rate ?? 0,
      p50_ms: timings?.med ?? null, p95_ms: timings?.['p(95)'] ?? null }];
  }));
  return {
    [__ENV.RAG_LOAD_OUTPUT]: JSON.stringify({ rows, requested_rate: rate, requested_seconds: seconds,
      duration_ms: state.testRunDurationMs, iterations: metrics.iterations?.values.count ?? 0,
      dropped_iterations: metrics.dropped_iterations?.values.count ?? 0 }, null, 2),
  };
}
