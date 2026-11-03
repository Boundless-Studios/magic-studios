import { describe, expect, it } from 'vitest'
import { createRequestQueue } from './requestQueue'

function deferred<T>() {
  let resolve!: (value: T) => void
  const promise = new Promise<T>((r) => { resolve = r })
  return { promise, resolve }
}

describe('createRequestQueue', () => {
  it('holds calls beyond the limit until one finishes', async () => {
    const queue = createRequestQueue({ limit: 2, retries: 0, baseDelayMs: 0 })
    const gates = [deferred<Response>(), deferred<Response>(), deferred<Response>()]
    let started = 0
    const runs = gates.map((gate) => queue.run(() => { started += 1; return gate.promise }, async (r) => r.status))
    await Promise.resolve()
    expect(started).toBe(2)
    expect(queue.inFlight).toBe(2)
    gates[0].resolve(new Response(null, { status: 200 }))
    await runs[0]
    expect(started).toBe(3)
    gates[1].resolve(new Response(null, { status: 200 }))
    gates[2].resolve(new Response(null, { status: 200 }))
    expect(await Promise.all(runs)).toEqual([200, 200, 200])
    expect(queue.inFlight).toBe(0)
  })

  it('retries a 429 after the backoff, honouring retry-after', async () => {
    const slept: number[] = []
    const queue = createRequestQueue({ limit: 1, retries: 3, baseDelayMs: 100, sleep: async (ms) => { slept.push(ms) } })
    const responses = [
      new Response(null, { status: 429, headers: { 'retry-after': '2' } }),
      new Response(null, { status: 429 }),
      new Response('{"ok":true}', { status: 200 }),
    ]
    const result = await queue.run(async () => responses.shift()!, (r) => r.json())
    expect(result).toEqual({ ok: true })
    expect(slept).toEqual([2000, 200])
  })

  it('gives up after the retry budget and hands the 429 to the reader', async () => {
    const queue = createRequestQueue({ limit: 1, retries: 1, baseDelayMs: 0, sleep: async () => {} })
    const status = await queue.run(async () => new Response(null, { status: 429 }), async (r) => r.status)
    expect(status).toBe(429)
  })
})
