import { createHash } from 'node:crypto'
import { mkdirSync, renameSync, writeFileSync } from 'node:fs'
import { tmpdir } from 'node:os'
import path from 'node:path'
import { deflateSync } from 'node:zlib'

// The character image the flows upload. The suite draws it rather than keeping art in Git:
// a 512 px RGBA PNG of a shaded disc on a transparent canvas, clear of every edge and covering
// about half of it, so it passes the subject checks (studio/quality.py) like a real character.
const SIZE = 512
const RADIUS = 200

function crc32(bytes: Buffer): number {
  let crc = ~0
  for (const byte of bytes) {
    crc ^= byte
    for (let bit = 0; bit < 8; bit += 1) crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1))
  }
  return ~crc >>> 0
}

function chunk(type: string, data: Buffer): Buffer {
  const length = Buffer.alloc(4)
  length.writeUInt32BE(data.length)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body))
  return Buffer.concat([length, body, crc])
}

export function subjectPngBytes(): Buffer {
  const stride = SIZE * 4 + 1
  const rows = Buffer.alloc(stride * SIZE) // each row starts with filter 0 (none)
  const centre = SIZE / 2
  for (let y = 0; y < SIZE; y += 1) {
    for (let x = 0; x < SIZE; x += 1) {
      const dx = x + 0.5 - centre
      const dy = y + 0.5 - centre
      const coverage = Math.min(1, Math.max(0, RADIUS + 0.5 - Math.hypot(dx, dy)))
      if (coverage === 0) continue
      const light = 1 - Math.hypot(dx + 70, dy + 70) / (2 * RADIUS) // lit from the upper left
      const offset = y * stride + 1 + x * 4
      rows[offset] = Math.round(40 + 60 * light)
      rows[offset + 1] = Math.round(120 + 100 * light)
      rows[offset + 2] = Math.round(200 + 55 * light)
      rows[offset + 3] = Math.round(255 * coverage)
    }
  }
  const header = Buffer.alloc(13)
  header.writeUInt32BE(SIZE, 0)
  header.writeUInt32BE(SIZE, 4)
  header[8] = 8 // bits per channel
  header[9] = 6 // RGBA
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    chunk('IHDR', header),
    chunk('IDAT', deflateSync(rows)),
    chunk('IEND', Buffer.alloc(0)),
  ])
}

function writeSubjectPng(): string {
  const bytes = subjectPngBytes()
  const directory = path.join(tmpdir(), 'magic-studios-e2e')
  const file = path.join(directory, `subject-${createHash('sha256').update(bytes).digest('hex').slice(0, 16)}.png`)
  mkdirSync(directory, { recursive: true })
  // Workers import this at once; each renames its own complete copy over the same name.
  const partial = `${file}.${process.pid}`
  writeFileSync(partial, bytes)
  renameSync(partial, file)
  return file
}

/** Path to the generated subject PNG, for `setInputFiles`, `readFile` and ffmpeg. */
export const subjectPng = writeSubjectPng()
