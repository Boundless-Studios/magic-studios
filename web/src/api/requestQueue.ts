/**
 * A small in-browser request queue: at most `limit` API calls in flight, the rest wait their
 * turn, and a 429 (the service at capacity) is retried after a backoff instead of surfacing as
 * an empty list. The studio API deliberately runs on one Cloud Run instance, so the browser is
 * the right place to smooth a page-load burst into a queue.
 */
export interface RequestQueueOptions {
  limit: number
  retries: number
  baseDelayMs: number
  sleep?: (ms: number) => Promise<void>
}

const defaultSleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms))

export function createRequestQueue(options: RequestQueueOptions) {
  const sleep = options.sleep ?? defaultSleep
  let active = 0
  const waiting: Array<() => void> = []

  function acquire(): Promise<void> {
    if (active < options.limit) { active += 1; return Promise.resolve() }
    return new Promise((resolve) => waiting.push(() => { active += 1; resolve() }))
  }
  function release() {
    active -= 1
    waiting.shift()?.()
  }

  return {
    async run<T>(fn: () => Promise<Response>, read: (response: Response) => Promise<T>): Promise<T> {
      for (let attempt = 0; ; attempt += 1) {
        await acquire()
        let response: Response
        try { response = await fn() } finally { release() }
        if (response.status === 429 && attempt < options.retries) {
          const retryAfter = Number(response.headers.get('retry-after'))
          await sleep(Number.isFinite(retryAfter) && retryAfter > 0 ? retryAfter * 1000 : options.baseDelayMs * 2 ** attempt)
          continue
        }
        return read(response)
      }
    },
    /** Test seam: how many calls are in flight right now. */
    get inFlight() { return active },
  }
}

export const apiQueue = createRequestQueue({ limit: 6, retries: 3, baseDelayMs: 400 })
