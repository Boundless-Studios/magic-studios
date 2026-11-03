import { useEffect, useState } from 'react'

// The file names carry the version; a backend test checks they match what the build makes.
export const MCP_WHEEL_URL = 'https://magicstudios.ai/studio/downloads/magic_studios_mcp-0.2.2-py3-none-any.whl'
export const SKILL_ZIP_URL = 'https://magicstudios.ai/studio/downloads/magic-studios-skill-0.2.2.zip'

type Download = { name: string, sha256: string }

/** Each download's SHA-256, as the server that serves it reports it. */
export function DownloadChecksums() {
  const [files, setFiles] = useState<Download[] | null>(null)
  useEffect(() => {
    let live = true
    fetch('/studio/downloads')
      .then((response) => (response.ok ? response.json() : { files: [] }))
      .then((body: { files?: Download[] }) => { if (live) setFiles(body.files ?? []) })
      .catch(() => { if (live) setFiles([]) })
    return () => { live = false }
  }, [])
  if (!files?.length) return <p>Each file&rsquo;s SHA-256 is listed at <a href="/studio/downloads">/studio/downloads</a>.</p>
  return (
    <div>
      <p>SHA-256 checksums:</p>
      <ul className="guide__checksums">
        {files.map((file) => <li key={file.name}><code>{file.name}</code><br /><code>{file.sha256}</code></li>)}
      </ul>
    </div>
  )
}
