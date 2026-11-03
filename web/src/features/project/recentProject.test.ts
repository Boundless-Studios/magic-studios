import { expect, it } from 'vitest'
import { availableRecentProject, recentProjectKey } from './recentProject'

it('resumes accessible movies and animations at their last step', () => {
  expect(availableRecentProject('/movies/film/shots', [], ['film'])).toBe('/movies/film/shots')
  expect(availableRecentProject('/projects/animation/motions', ['animation'], [])).toBe('/projects/animation/motions')
})
it('rejects missing projects and unsafe stored destinations', () => {
  for (const path of [null, '/movies/deleted/story', '//example.com', '/account/machines', '/projects/animation/../../login']) {
    expect(availableRecentProject(path, ['animation'], ['film'])).toBeNull()
  }
})
it('keeps account preferences separate', () => {
  expect(recentProjectKey('alice')).not.toBe(recentProjectKey('bob'))
})
