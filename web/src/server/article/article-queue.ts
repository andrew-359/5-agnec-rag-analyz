import amqp, { type ConfirmChannel } from 'amqplib'
import type { ArticleStageDeliveryStore, StagePublication } from './article-stage-delivery-store.ts'

export type ArticleDelivery = { url: string; queue: string; shutdown: AbortSignal }

async function publishStage(channel: ConfirmChannel, queue: string, stage: StagePublication) {
  let returned = false
  const onReturn = () => { returned = true }
  channel.once('return', onReturn)
  try {
    await new Promise<void>((resolve, reject) => {
      const body = Buffer.from(`${stage.id}:${stage.generation}`)
      channel.sendToQueue(queue, body, { persistent: true, mandatory: true, contentType: 'text/plain' },
        error => error ? reject(error) : resolve())
    })
    if (returned) throw new Error('RabbitMQ вернул сообщение без доставки в очередь.')
  } finally {
    channel.off('return', onReturn)
  }
}

export function startArticleDelivery(
  runs: ArticleStageDeliveryStore, { url, queue, shutdown }: ArticleDelivery,
) {
  let inFlight = false
  let lastError: string | undefined
  const dispatch = async () => {
    if (shutdown.aborted) return
    runs.recoverInterrupted()
    if (inFlight) return
    inFlight = true
    try {
      const pending = runs.pendingPublication()
      if (!pending.length) return
      const signal = AbortSignal.any([AbortSignal.timeout(8000), shutdown])
      const options = { timeout: 5000, signal }
      const connection = await amqp.connect(url, options)
      connection.on('error', () => {})
      try {
        const channel = await connection.createConfirmChannel()
        channel.on('error', () => {})
        await channel.assertQueue(queue, { durable: true })
        for (const stage of pending) {
          await publishStage(channel, queue, stage)
          runs.recordPublication(stage)
        }
        lastError = undefined
      } finally {
        await connection.close().catch(() => {})
      }
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error)
      if (!shutdown.aborted && lastError !== message) console.error('Доставка этапов прогона:', message)
      lastError = message
    } finally {
      inFlight = false
    }
  }
  const tick = () => { void dispatch().catch(error => console.error('Восстановление этапов прогона:', error.message)) }
  const timer = setInterval(tick, 750).unref()
  shutdown.addEventListener('abort', () => clearInterval(timer), { once: true })
  tick()
}
