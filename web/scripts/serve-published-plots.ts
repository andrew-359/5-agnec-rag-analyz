/** Small viewer for published packages; the production server mounts the same route. */
import { serve } from '@hono/node-server';
import { fileURLToPath } from 'node:url';
import { plotPublications } from '../src/server/plot-publications.ts';

const root = process.env.RAG_PUBLISHED_PLOTS ?? fileURLToPath(new URL('../../runs/published-plots', import.meta.url));
serve({ fetch: plotPublications(root).fetch, hostname: '127.0.0.1', port: Number(process.env.PORT ?? 4313) },
  ({ port }) => console.log(`Published plots: http://127.0.0.1:${port}/published/<version>/index.html`));
