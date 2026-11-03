import { useRef, useState, type ReactNode } from 'react'

export interface SequenceItem { id: string; name: string; detail: string; image?: string }

export function MovieSequence({ items, noun = 'shot', help, empty, keepHint = 'Save movie to keep this order.', footer = 'Order saves with your movie.', disabled, onMove, onOpen, activeId, children }: {
  activeId?: string; items: SequenceItem[]; disabled: boolean
  noun?: string; help: string; empty: ReactNode; keepHint?: string; footer?: string; children?: ReactNode
  onMove: (from: number, to: number) => boolean; onOpen: (id: string) => void
}) {
  const drag = useRef<{ from: number; to: number; pointer: number } | null>(null)
  const suppressClick = useRef(false)
  const [target, setTarget] = useState<number | null>(null)
  const [announcement, setAnnouncement] = useState('')
  function move(from: number, to: number) {
    if (to < 0 || to >= items.length || from === to) return
    if (onMove(from, to)) setAnnouncement(`${items[from].name} is now ${noun} ${to + 1}. ${keepHint}`)
    else setAnnouncement(`Keep continuation ${noun}s after the ${noun}s they continue.`)
  }
  return <aside className="movie-panel movie-sequence" aria-label="Movie sequence">
    <div className="movie-section-title"><h3>Movie sequence</h3><span>{items.length} {noun}s</span></div>
    <p id="movie-sequence-help">{help}</p>
    <ol className="movie-sequence-list">
      {items.map((item, index) => <li key={item.id} data-sequence-index={index} className={target === index ? 'is-drop-target' : ''}>
        <button type="button" className={`movie-sequence-card${activeId === item.id ? ' is-active' : ''}`} aria-current={activeId === item.id ? 'true' : undefined} disabled={disabled} aria-label={`Arrange ${item.name}, ${noun} ${index + 1}`} aria-describedby="movie-sequence-help"
          onPointerDown={(event) => {
            if (event.button !== 0) return
            suppressClick.current = false
            drag.current = { from: index, to: index, pointer: event.pointerId }
            event.currentTarget.setPointerCapture(event.pointerId)
          }}
          onPointerMove={(event) => {
            if (!drag.current || drag.current.pointer !== event.pointerId) return
            const row = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-sequence-index]')
            if (row && event.currentTarget.closest('ol')?.contains(row)) {
              drag.current.to = Number(row.getAttribute('data-sequence-index'))
              setTarget(drag.current.to)
            }
            const list = event.currentTarget.closest('ol')
            if (list) {
              const bounds = list.getBoundingClientRect()
              if (event.clientY > bounds.bottom - 35) list.scrollTop += 18
              if (event.clientY < bounds.top + 35) list.scrollTop -= 18
            }
          }}
          onPointerUp={(event) => {
            const current = drag.current
            drag.current = null; setTarget(null)
            if (current && current.from !== current.to) {
              suppressClick.current = true
              const row = document.elementFromPoint(event.clientX, event.clientY)?.closest('[data-sequence-index]')
              if (row && event.currentTarget.closest('ol')?.contains(row)) move(current.from, Number(row.getAttribute('data-sequence-index')))
            }
          }}
          onPointerCancel={() => { drag.current = null; setTarget(null); suppressClick.current = true }}
          onKeyDown={(event) => {
            if (event.key === 'Escape') { drag.current = null; setTarget(null); suppressClick.current = true }
            if (event.key === 'ArrowUp' || event.key === 'ArrowDown') { event.preventDefault(); move(index, index + (event.key === 'ArrowUp' ? -1 : 1)) }
          }}
          onClick={() => { if (!suppressClick.current) onOpen(item.id); suppressClick.current = false }}>
          <span className="movie-sequence-thumb">{item.image ? <img draggable={false} src={item.image} alt="" /> : <span aria-hidden="true">{String(index + 1).padStart(2, '0')}</span>}</span>
          <span className="movie-sequence-caption"><strong>{item.name}</strong><small>{String(index + 1).padStart(2, '0')} · {item.detail}</small></span>
          <span className="movie-sequence-grip" aria-hidden="true">⠿</span>
        </button>
      </li>)}
    </ol>
    {!items.length && empty}
    {children}
    <p className="movie-muted">{footer}</p>
    <p role="status" className="movie-sequence-announcement">{announcement}</p>
  </aside>
}
