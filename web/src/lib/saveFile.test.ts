import { expect, it, vi } from 'vitest'
import { saveFromUrl } from './saveFile'

it('clicks a throwaway anchor pointing at the signed URL and removes it again', () => {
  const clicked: { href: string; rel: string; attached: boolean }[] = []
  const originalClick = HTMLAnchorElement.prototype.click
  HTMLAnchorElement.prototype.click = function click(this: HTMLAnchorElement) {
    clicked.push({ href: this.href, rel: this.rel, attached: this.isConnected })
  }
  try {
    saveFromUrl('https://assets.invalid/ember-idle-take-1.png')
  } finally {
    HTMLAnchorElement.prototype.click = originalClick
  }

  expect(clicked).toEqual([{ href: 'https://assets.invalid/ember-idle-take-1.png', rel: 'noopener', attached: true }])
  expect(document.querySelectorAll('a')).toHaveLength(0)
})

it('never opens a new tab, so a popup blocker cannot swallow the download', () => {
  const open = vi.spyOn(window, 'open').mockReturnValue(null)
  const originalClick = HTMLAnchorElement.prototype.click
  HTMLAnchorElement.prototype.click = function click() {}
  try {
    saveFromUrl('https://assets.invalid/export.zip')
  } finally {
    HTMLAnchorElement.prototype.click = originalClick
    open.mockRestore()
  }

  expect(open).not.toHaveBeenCalled()
})
