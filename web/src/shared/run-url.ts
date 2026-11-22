export function runUrl(id: string, stageId?: string) {
  const params = new URLSearchParams({ articleRun: id })
  if (stageId) params.set('articleStage', stageId)
  return `/?${params}`
}
