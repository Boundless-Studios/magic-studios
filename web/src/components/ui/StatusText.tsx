import type { ReactNode } from 'react'

type StatusTone = 'neutral' | 'success' | 'warning' | 'error'

export interface StatusTextProps {
  children: ReactNode
  tone?: StatusTone
}

export function StatusText({ children, tone = 'neutral' }: StatusTextProps) {
  return (
    <span className={`status-text status-text--${tone}`} role="status">
      <span aria-hidden="true" className="status-text__dot" />
      {children}
    </span>
  )
}
