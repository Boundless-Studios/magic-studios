import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { useState } from 'react'
import { expect, it } from 'vitest'
import type { ProjectSetup } from '../../api/types'
import { recommendedSetup } from './setupModel'
import { SetupChoices } from './SetupChoices'
function Harness({ original }: { original: ProjectSetup }) { const [setup, setSetup] = useState(original); return <><SetupChoices setup={setup} original={original} onChange={setSetup} /><output data-testid="request">{JSON.stringify(setup)}</output></> }
it('restores current motion identity before apply and allocates fresh identity after persisted removal', async () => {
  const user = userEvent.setup(); const original = recommendedSetup('single', null, 'art'); const { unmount } = render(<Harness original={original} />)
  await user.click(screen.getByRole('checkbox', { name: /Walk/ })); await user.click(screen.getByRole('checkbox', { name: /Walk/ })); expect(screen.getByTestId('request')).toHaveTextContent('single-walk'); unmount()
  render(<Harness original={{ ...original, views: original.views.map((view) => ({ ...view, motions: view.motions.filter((motion) => motion.preset !== 'walk') })) }} />)
  await user.click(screen.getByRole('checkbox', { name: /Walk/ })); const request = JSON.parse(screen.getByTestId('request').textContent!); const walk = request.views[0].motions.find((motion: { preset: string }) => motion.preset === 'walk'); expect(walk.motion_id).not.toBe('single-walk'); expect(walk.semantic_slot).toBe('walk')
})
it('allocates distinct custom identities after removing another custom action', async () => {
  const user = userEvent.setup(); render(<Harness original={recommendedSetup('single', null, 'art', true)} />)
  async function add(name: string) { await user.type(screen.getByLabelText('Custom animation name for Original view'), name); await user.type(screen.getByLabelText('Custom animation action for Original view'), 'waves once'); await user.click(screen.getByRole('button', { name: 'Add custom' })) }
  await add('A'); await add('B'); await user.click(screen.getByRole('checkbox', { name: 'A' })); await add('B'); const request = JSON.parse(screen.getByTestId('request').textContent!); expect(new Set(request.views[0].motions.map((motion: { motion_id: string }) => motion.motion_id)).size).toBe(2)
})
