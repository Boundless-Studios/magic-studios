import type { ButtonHTMLAttributes, KeyboardEvent, MouseEvent, ReactNode } from 'react'

type ButtonVariant = 'primary' | 'secondary'

export interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  children: ReactNode
  variant?: ButtonVariant
}

export function Button({
  'aria-disabled': ariaDisabled,
  children,
  className = '',
  onClick,
  onKeyDown,
  variant = 'secondary',
  ...props
}: ButtonProps) {
  const isUnavailable = ariaDisabled === true || ariaDisabled === 'true'

  function handleClick(event: MouseEvent<HTMLButtonElement>) {
    if (isUnavailable) {
      event.preventDefault()
      event.stopPropagation()
      return
    }
    onClick?.(event)
  }

  function handleKeyDown(event: KeyboardEvent<HTMLButtonElement>) {
    if (isUnavailable && (event.key === 'Enter' || event.key === ' ')) {
      event.preventDefault()
      event.stopPropagation()
      return
    }
    onKeyDown?.(event)
  }

  return (
    <button
      aria-disabled={ariaDisabled}
      className={`button button--${variant} ${className}`.trim()}
      onClick={handleClick}
      onKeyDown={handleKeyDown}
      type="button"
      {...props}
    >
      {children}
    </button>
  )
}
