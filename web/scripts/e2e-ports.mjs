// Prints two free localhost ports as `E2E_PORT=<api>` and `E2E_WEB_PORT=<web>` lines, ready
// for `>> $GITHUB_ENV`. Asking the kernel for free ports at job start is the only scheme that
// cannot collide across workflows, reruns, or a neighbour job on the same self-hosted runner.
import { createServer } from 'node:net'

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer()
    server.unref()
    server.on('error', reject)
    server.listen(0, '127.0.0.1', () => {
      const { port } = server.address()
      server.close(() => resolve(port))
    })
  })
}

const api = await freePort()
let web = await freePort()
while (web === api) web = await freePort()
process.stdout.write(`E2E_PORT=${api}\nE2E_WEB_PORT=${web}\n`)
