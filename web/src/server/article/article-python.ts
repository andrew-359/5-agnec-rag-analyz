import { execFile } from 'node:child_process'
import { access } from 'node:fs/promises'
import { join } from 'node:path'

const maxOutputBytes = 16 * 1024 * 1024

export function createArticlePythonRunner(root: string) {
  const venv = join(root, '.venv/bin/python')
  const python = process.env.RAG_PYTHON || venv

  return async function pythonTask(args: string[], timeout: number, acceptRemarks = false, input?: string) {
    const pythonAvailable = () => access(venv).then(() => true, () => false)
    const command = process.env.RAG_PYTHON ? python : (await pythonAvailable() ? venv : 'python3')

    return new Promise<Buffer>((resolve, reject) => {
      const child = execFile(command, args, {
        cwd: root,
        timeout,
        killSignal: 'SIGKILL',
        maxBuffer: maxOutputBytes,
        encoding: 'buffer',
      }, (error, stdout, stderr) => {
        if (!error || (acceptRemarks && error.code === 2 && stdout.length > 0)) return resolve(stdout)

        const detail = stderr.toString().trim().split('\n').at(-1)
        if (error.killed) return reject(new Error('Обработка заняла слишком много времени.'))
        if (error.code === 'ENOENT') return reject(new Error('Python не найден. Проверьте RAG_PYTHON.'))
        if (detail?.startsWith('ModuleNotFoundError:')) {
          return reject(new Error('Не хватает зависимостей Python. Установите requirements.txt в .venv или укажите RAG_PYTHON.'))
        }

        reject(new Error(detail?.slice(0, 500) || 'Не удалось обработать PDF.'))
      })
      if (input !== undefined) {
        child.stdin?.on('error', reject)
        child.stdin?.end(input)
      }
    })
  }
}
