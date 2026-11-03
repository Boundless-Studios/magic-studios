/**
 * Saves the file behind a signed URL. The backend signs download URLs with
 * `Content-Disposition: attachment`, so a same-tab navigation downloads without leaving the
 * page -- and unlike `window.open` after an `await`, a synthetic anchor click is never blocked
 * by a popup blocker.
 */
export function saveFromUrl(url: string): void {
  const anchor = document.createElement('a')
  anchor.href = url
  anchor.rel = 'noopener'
  document.body.append(anchor)
  anchor.click()
  anchor.remove()
}
