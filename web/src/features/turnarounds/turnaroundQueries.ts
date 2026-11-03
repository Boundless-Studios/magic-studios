import { useQuery } from '@tanstack/react-query'
import { studioApi } from '../../api/client'

/** The project's direction drafts; the strip and the panel share one query. */
export function useViewDrafts(projectId: string) {
  return useQuery({ queryKey: ['view-drafts', projectId], queryFn: () => studioApi.viewDrafts(projectId).then((page) => page.drafts) })
}
