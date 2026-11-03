import { QueryClient, QueryClientProvider } from '@tanstack/react-query'
import type { ReactNode } from 'react'
import { MemoryRouter } from 'react-router-dom'

import type { ProjectResponse } from '../api/types'

export const projectFixture: ProjectResponse = {
  project_id: 'project-1', revision: 4, manifest_digest: 'manifest-4', updated_at: '2026-08-31T00:00:00Z',
  active_operations: [], primary_action: 'add_subject',
  manifest: {
    project_id: 'project-1', name: 'Ember Knight', owner_id: 'actor-1', external_subject: null,
    subject: {
      current_revision: null, assessment: null,
      approval: { state: 'not_ready', assessment_id: null, approver_id: null, policy_version: null, artifact_sha256: null, manifest_digest: null, waived_finding_ids: [], waiver_reason: null },
    },
    composition: { current: null },
    motions: [], set_assessment: null, set_assessment_id: null, set_assessment_digest: null, set_assessment_fresh: false,
    set_approval: { state: 'not_ready', assessment_id: null, approver_id: null, policy_version: null, artifact_sha256: null, manifest_digest: null, waived_finding_ids: [], waiver_reason: null },
    set_approval_fresh: false,
  },
}

export function queryWrapper() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false }, mutations: { retry: false } } })
  return function Wrapper({ children }: { children: ReactNode }) {
    return <MemoryRouter initialEntries={['/projects/project-1/subject']}><QueryClientProvider client={client}>{children}</QueryClientProvider></MemoryRouter>
  }
}
