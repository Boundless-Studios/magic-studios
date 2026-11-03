import { useQuery, useQueryClient } from '@tanstack/react-query'
import { useEffect, useRef, useState } from 'react'
import { useLocation } from 'react-router-dom'
import { studioApi } from '../../api/client'
import { useProject, useProjectMutation } from '../../api/queries'
import type { Release, TargetInfo } from '../../api/types'
import { Button } from '../../components/ui/Button'
import { deriveSetFindings } from './setFindings'
import { SetFindings } from './SetFindingsSection'
import { SetStrip } from './SetStrip'
import { ReleaseCard, type ExportProfileOption } from './ReleaseCard'
import { recordReleaseCreated } from './releaseLog'

const PNG_JSON_PROFILE: ExportProfileOption = { id: 'png-json', label: 'PNG + JSON', description: 'Production-ready PNG frames with a JSON manifest.' }

function exportProfiles(targets: TargetInfo[] | undefined): ExportProfileOption[] {
  return [
    PNG_JSON_PROFILE,
    ...(targets ?? []).map((target) => ({
      id: target.system,
      label: target.display_name,
      description: `PNG + JSON, plus a ${target.display_name} adapter package (${target.system}/package.json) and, when this project has an external subject, ${target.system}/bindings.json.`,
    })),
  ]
}

export function ReleaseWorkspace({ projectId }: { projectId: string }) {
  const project = useProject(projectId)
  const releases = useQuery({ queryKey: ['releases', projectId], queryFn: () => studioApi.releases(projectId) })
  const targets = useQuery({ queryKey: ['targets'], queryFn: studioApi.targets })
  const client = useQueryClient()
  const location = useLocation()
  const [created, setCreated] = useState<Release | null>(null)
  const [name, setName] = useState('')
  const [reason, setReason] = useState('')
  const [defaultProfile, setDefaultProfile] = useState<string | null>(null)
  const nameInputRef = useRef<HTMLInputElement>(null)
  const newestReleaseRef = useRef<HTMLElement>(null)

  const assess = useProjectMutation(projectId, () => studioApi.assessSet(projectId, project.data!.revision))
  const approve = useProjectMutation(projectId, () => studioApi.approveSet(projectId, project.data!.revision, reason || undefined))
  const create = useProjectMutation(projectId, (releaseName: string) => studioApi.createRelease(projectId, project.data!.revision, releaseName))

  const profiles = exportProfiles(targets.data)
  // A target profile is preselected once the project's own external subject names a configured
  // target (T22); the operator can still change it per release -- this only seeds the default.
  useEffect(() => { if (defaultProfile === null && project.data) setDefaultProfile(project.data.manifest.external_subject?.system ?? 'png-json') }, [defaultProfile, project.data])

  // Header deep links (studio-ux-flow.md section 5): "Release" focuses the name field, "Export"
  // scrolls the newest release's export controls into view.
  useEffect(() => {
    const state = location.state as { focusReleaseName?: boolean; scrollToExport?: boolean } | null
    if (state?.focusReleaseName) nameInputRef.current?.focus()
    if (state?.scrollToExport) newestReleaseRef.current?.scrollIntoView({ block: 'start' })
  }, [location.state])

  if (project.isPending) return <section className="workspace-state" role="status">Opening releases</section>
  if (!project.data) return <section className="workspace-state">Releases unavailable</section>

  const manifest = project.data.manifest
  const setApproved = manifest.set_approval_fresh
  const findings = deriveSetFindings(manifest)
  const ordered = [...(releases.data ?? [])].sort((a, b) => b.source_revision - a.source_revision || b.release_id.localeCompare(a.release_id))
  const allReleases = created && !ordered.some((release) => release.release_id === created.release_id) ? [created, ...ordered] : ordered
  const notice = assess.notice || approve.notice || create.notice
  const error = assess.error || approve.error || create.error || releases.error

  async function approveSet() { try { await approve.mutateAsync(); setReason('') } catch { /* mutation renders the safe error */ } }
  async function createRelease() {
    try {
      const release = await create.mutateAsync(name) as Release
      recordReleaseCreated(release.release_id)
      setCreated(release)
      setName('')
      await client.invalidateQueries({ queryKey: ['releases', projectId] })
    } catch { /* mutation renders the safe error */ }
  }

  return <section aria-labelledby="release-title" className="workspace">
    <header className="workspace-heading">
      <div>
        <p className="eyebrow">Release</p>
        <h2 id="release-title">Approve the set and ship it</h2>
        <p>A release is an immutable record of the approved subject and motion revisions. Exporting and delivering creates packages from it.</p>
      </div>
    </header>
    {(notice || error) && <p className="notice" role="status">{notice ?? (error instanceof Error ? error.message : 'That release action could not be completed. Nothing was published.')}</p>}

    <SetStrip views={manifest.views} externalSubject={manifest.external_subject} findingsCount={findings.length} motions={manifest.motions} projectId={projectId} projectName={manifest.name} subjectRevision={manifest.subject.current_revision} />

    <section className="release-card" aria-labelledby="set-findings-title">
      <span className="step-number">02</span>
      <h3 id="set-findings-title">Set findings</h3>
      {!manifest.set_assessment_fresh
        ? <Button onClick={() => assess.mutate()} variant="primary">Run quality checks</Button>
        : <>
          <SetFindings findings={findings} projectId={projectId} />
          <label>Note for the record (optional)<textarea onChange={(event) => setReason(event.target.value)} value={reason} /></label>
          <Button disabled={setApproved} onClick={approveSet} variant="primary">
            {setApproved ? (manifest.set_approval.state === 'waived' ? 'Animation set approved with findings acknowledged' : 'Animation set approved') : 'Approve animation set'}
          </Button>
        </>}
    </section>

    <section className="release-card" aria-labelledby="releases-title">
      <span className="step-number">03</span>
      <h3 id="releases-title">Releases</h3>
      <label>Release name<input onChange={(event) => setName(event.target.value)} placeholder="Spring launch" ref={nameInputRef} value={name} /></label>
      <Button aria-disabled={!setApproved || !name.trim()} onClick={createRelease} variant="primary">Create immutable release</Button>
      {allReleases.map((release, index) => <ReleaseCard
        defaultProfile={defaultProfile ?? 'png-json'}
        key={release.release_id}
        profiles={profiles}
        projectId={projectId}
        ref={index === 0 ? newestReleaseRef : undefined}
        release={release}
        targets={targets.data}
      />)}
    </section>
  </section>
}
