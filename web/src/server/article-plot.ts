import type { FigureStatus } from '../shared/article-plot.ts'

type PlotProfile = {
  page: number
  figure: string
  panel: string
  series: string
  x: string
  y: string
  strategy: 'vector-line' | 'vector-scatter' | 'raster-line'
  options?: string[]
}

// Calibration and known axis labels apply to these exact PDF snapshots.
const pilots: Record<string, PlotProfile> = {
  '7fcbb21b0bdf5bb222b347d0b5e8d6587aa8af5c5f855f62322d0d7ad5427d3c': {
    page: 8, figure: '3', panel: 'a', series: 'HotpotQA', x: 'anchors', y: 'EM', strategy: 'vector-line',
  },
  '3024cfb478f8c334441beb3db351e374551b31e8304a4d98b2c5e5f8073612d8': {
    page: 16, figure: '10', panel: '1', series: 'Wizard of Wikipedia / GPT-4o-mini',
    x: 'Knowledge Precision', y: 'Answer F1', strategy: 'vector-scatter', options: ['--marker-size', '1.494'],
  },
  'a16d5237120a18d18488f4db3d7ae3fbc5ff628a90b794bd007451ae53472b7d': {
    page: 9, figure: '2', panel: 'a', series: 'PPO', x: 'Шаг обучения', y: 'EM (доля)', strategy: 'raster-line',
    options: ['--raster-config', '.scratch/plot-extraction/d013-raster-config.json'],
  },
}

export function figureStatus(sha256: string, page: number, label: string | null): FigureStatus {
  const pilot = pilots[sha256]
  if (pilot?.page === page && pilot.figure === label) return 'data'
  if (sha256 === '7fcbb21b0bdf5bb222b347d0b5e8d6587aa8af5c5f855f62322d0d7ad5427d3c') {
    if ((page === 2 && label === '1') || (page === 3 && label === '2')) return 'illustration'
    if ((page === 8 && (label === '4' || label === '5')) || (page === 16 && label === '6')) return 'data'
  }
  return 'review'
}

export function rasterPilot(sha256: string) {
  const pilot = pilots[sha256]
  return pilot?.strategy === 'raster-line' ? { page: pilot.page, figure: pilot.figure } : undefined
}

export function plotArguments(sha256: string, page: number, figure: string, pdf: string) {
  const pilot = pilots[sha256]
  const profile: PlotProfile = pilot?.page === page && pilot.figure === figure ? pilot : {
    page, figure, panel: '1', series: '', x: 'X', y: 'Y', strategy: 'vector-line',
  }
  return ['-m', 'rag_mvp.plot_extract', pdf, '--page', String(page), '--figure', figure,
    '--panel', profile.panel, '--series', profile.series, '--x-axis', profile.x, '--y-axis', profile.y,
    '--strategy', profile.strategy, ...(profile.options ?? []), '--out', '-']
}
