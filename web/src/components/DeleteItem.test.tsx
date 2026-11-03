import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import { DeleteItem } from './DeleteItem'

describe('DeleteItem', () => {
  it('stays disabled until the exact name is typed, then deletes', async () => {
    const onDelete = vi.fn().mockResolvedValue(undefined)
    const onDeleted = vi.fn()
    render(<DeleteItem kind="project" name="Ember Knight" onDelete={onDelete} onDeleted={onDeleted} />)

    await userEvent.click(screen.getByRole('button', { name: 'Delete Ember Knight' }))
    const confirm = screen.getByRole('button', { name: 'Delete permanently' })
    expect(confirm).toBeDisabled()
    await userEvent.type(screen.getByLabelText('Type the name to confirm'), 'ember knight')
    expect(confirm).toBeDisabled()
    await userEvent.clear(screen.getByLabelText('Type the name to confirm'))
    await userEvent.type(screen.getByLabelText('Type the name to confirm'), 'Ember Knight')
    await userEvent.click(confirm)

    expect(onDelete).toHaveBeenCalledOnce()
    expect(onDeleted).toHaveBeenCalledOnce()
  })

  it('shows why a delete failed and leaves the item in place', async () => {
    const onDeleted = vi.fn()
    render(<DeleteItem kind="movie" name="Heist" onDelete={() => Promise.reject(new Error('Storage is unavailable.'))} onDeleted={onDeleted} />)

    await userEvent.click(screen.getByRole('button', { name: 'Delete Heist' }))
    await userEvent.type(screen.getByLabelText('Type the name to confirm'), 'Heist')
    await userEvent.click(screen.getByRole('button', { name: 'Delete permanently' }))

    expect(await screen.findByRole('alert')).toHaveTextContent('Storage is unavailable.')
    expect(onDeleted).not.toHaveBeenCalled()
  })
})
