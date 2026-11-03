import type { StudioProject } from './ProjectShell'
import { studioApi } from '../../api/client'

export interface ProjectRepository {
  getById(projectId: string): Promise<StudioProject | null>
}

export const previewProjectRepository: ProjectRepository = {
  async getById(projectId) {
    try {
      const project = await studioApi.project(projectId)
      const releases = await studioApi.releases(projectId)
      return { id: project.project_id, name: project.manifest.name, saveState: 'Saved', releaseReady: project.manifest.set_approval_fresh, exportReady: releases.length > 0 }
    } catch (error) {
      if (error instanceof Error && 'status' in error && error.status === 404) return null
      throw error
    }
  },
}
