import { useEffect, useRef, useState, type ReactNode } from 'react'
import { useNavigate } from 'react-router-dom'
import { studioApi } from '../../api/client'
import { canStartTurnaround, type GenerationConfig, type Heading, type ProjectResponse, type ProjectSetup, type SetupPlan } from '../../api/types'
import { SetupChoices } from './SetupChoices'
import { artworkNeeded, headings, recommendedSetup, TEMPLATES, type Template } from './setupModel'
import './setup.css'
/** `account` is the signed-in account menu, so a new account can reach it before its first project. */
export function ArtFirstOnboarding({ onCancel, onMovie, onFlight, onAudio, account }: { onCancel?: () => void; onMovie: () => void; onFlight?: () => void; onAudio?: () => void; account?: ReactNode }) {
  const navigate = useNavigate(); const titleRef = useRef<HTMLHeadingElement>(null)
  const [step, setStep] = useState(0); const [file, setFile] = useState<File | null>(null); const [preview, setPreview] = useState(''); const [name, setName] = useState('')
  const [template, setTemplate] = useState<Template>('single'); const [facing, setFacing] = useState<Heading | null>(null); const [setup, setSetup] = useState<ProjectSetup>(recommendedSetup('single', null, null)); const [justImage, setJustImage] = useState(false)
  const [busy, setBusy] = useState(false); const [error, setError] = useState(''); const [saved, setSaved] = useState<ProjectResponse | null>(null); const [referenceId, setReferenceId] = useState<string | null>(null); const [plan, setPlan] = useState<SetupPlan | null>(null)
  useEffect(() => { if (!file) return; const url = URL.createObjectURL(file); setPreview(url); return () => URL.revokeObjectURL(url) }, [file])
  useEffect(() => { titleRef.current?.focus() }, [step])
  // Whether a machine can turn the front artwork into the other directions.
  const [machines, setMachines] = useState<GenerationConfig | null | undefined>(undefined)
  useEffect(() => { if (step !== 4 || machines !== undefined) return; studioApi.getGenerationConfig().then(setMachines, () => setMachines(null)) }, [step, machines])
  function chooseTemplate(value: Template) { setTemplate(value); setFacing(null); setSetup(recommendedSetup(value, null, file ? 'pending' : null, justImage)) }
  async function create(generateDirections = false) {
    if (!file) return
    setBusy(true); setError('')
    try {
      const capabilities = await studioApi.setupCapabilities()
      if (!capabilities.schema_versions.includes(1)) throw new Error('This server does not support artwork setup yet. Update Studio before creating this workspace.')
      let project = saved
      if (!project) {
        project = await studioApi.createProject(name.trim()); setSaved(project)
      }
      const revision = referenceId ?? (await studioApi.stageSetupReference(project.project_id, file)).revision_id
      setReferenceId(revision)
      const desired: ProjectSetup = { ...setup, identity_reference: { revision_id: revision }, views: setup.views.map((view) => ({ ...view, reference: view.heading === facing ? { revision_id: revision } : null })) }
      const nextPlan = await studioApi.previewSetup(project.project_id, project.revision, desired)
      if (nextPlan.blocked_operations.length) throw new Error('Wait for active operations to finish before saving setup.')
      setPlan(nextPlan)
      await studioApi.applySetup(project.project_id, nextPlan)
      navigate(`/projects/${project.project_id}/motions${generateDirections ? '?directions=generate' : ''}`, { replace: true })
    } catch (reason) { setError(reason instanceof Error ? reason.message : 'Could not create workspace. Try again.'); setPlan(null) }
    finally { setBusy(false) }
  }
  const titles = ['Upload your character', 'What are you making?', 'Which way does this art face?', 'Choose animations', 'Your workspace is ready to create']
  return <main className="art-onboarding"><header className="setup-header"><a href="/projects" className="setup-brand">✦ Magic Studios</a><button className="button" onClick={onMovie}>Make a movie instead</button>{onFlight && <button className="button" onClick={onFlight}>Make a scene flight instead</button>}{onAudio && <button className="button" onClick={onAudio}>Gather audio instead</button>}{account}</header>
    <nav aria-label="Setup progress" className="setup-progress">{['Artwork', 'Game view', 'Facing', 'Animations', 'Review'].map((label, index) => <span key={label} aria-current={step === index ? 'step' : undefined}>{index + 1}<small>{label}</small></span>)}</nav>
    <div className="setup-layout"><aside className="setup-art"><div className="setup-art__canvas">{preview ? <img src={preview} alt="Your supplied character artwork" /> : <div className="setup-art__empty"><span aria-hidden="true">✦</span><p>Your character starts here</p><small>One image is enough to begin.</small></div>}</div>{file && <><strong>{name || file.name}</strong><p>{template === 'single' ? 'Original view · facing preserved' : headings(template).find((item) => item.heading === facing)?.label ?? 'Choose its facing'}</p></>}<label className="button setup-upload">{file ? 'Replace artwork' : 'Choose character image'}<input type="file" accept="image/png,image/jpeg,image/webp" disabled={busy} onChange={(event) => { const next = event.target.files?.[0]; if (next) { setFile(next); setReferenceId(null); setSetup((current) => ({ ...current, views: current.views.map((view) => ({ ...view, reference: view.heading === facing ? { revision_id: 'pending' } : null })) })); setName(next.name.replace(/\.[^.]+$/, '').replace(/[-_]+/g, ' ')) } }} /></label><small>PNG, JPEG or WebP. We keep your supplied camera and pose.</small></aside>
    <section className="setup-content"><p className="eyebrow">A little setup. Your choices.</p><h1 ref={titleRef} tabIndex={-1}>{titles[step]}</h1>
      {step === 0 && <><p>Bring the artwork you want to animate. You can add other directions later.</p><label className="setup-name">Character / project name<input value={name} onChange={(event) => setName(event.target.value)} maxLength={200} /></label><p className="setup-note">Creating a workspace saves your artwork and choices. It does not generate animations.</p></>}
      {step === 1 && <><p>Choose a starting point for your game. Every suggestion stays editable.</p><div className="setup-template-grid">{TEMPLATES.map((item) => <button key={item.id} className="setup-template" aria-pressed={template === item.id} onClick={() => chooseTemplate(item.id)}><span className={`setup-template__illustration setup-template__illustration--${item.id}`} aria-hidden="true">{item.example}</span><strong>{item.title}</strong><small>{item.detail}</small><small className="setup-template__artwork">{item.artwork}</small></button>)}</div>{template !== 'single' && !justImage && <p className="setup-note" role="note">Studio does not draw the other directions for you yet. This image covers one of the {headings(template).length}; upload artwork for each of the others in your workspace before generating its animations.</p>}<label className="setup-note"><input type="checkbox" checked={justImage} onChange={(event) => { setJustImage(event.target.checked); setSetup(recommendedSetup(template, facing, file ? 'pending' : null, event.target.checked)) }} />Start with just this image, no animations yet</label></>}
      {step === 2 && (template === 'single' ? <div className="setup-note"><h2>Original view</h2><p>Keep exactly the view in your artwork. No compass direction is assigned.</p></div> : <><p>Choose the screen heading shown in your image. Diagonals describe heading, not an exact pose. Left and right never flip your pixels.</p><div className="setup-facing">{headings(template).map((item) => <button className="setup-template" key={item.heading} aria-pressed={facing === item.heading} onClick={() => { setFacing(item.heading); setSetup((current) => justImage ? recommendedSetup(template, item.heading, 'pending', true) : ({ ...current, views: current.views.map((view) => ({ ...view, reference: view.heading === item.heading ? { revision_id: 'pending' } : null })) })) }}><span aria-hidden="true">{item.arrow}</span>{item.label}</button>)}</div></>)}
      {step === 3 && <><p>Tick only what you want. Walk is a suggestion; Idle + Death, custom actions, and an empty set all work.</p>{template === 'eight' && <p className="setup-note">Suggested: eight Walks, four cardinal Attacks and South Idle. Death is optional.</p>}<button className="button" onClick={() => setSetup({ ...setup, views: setup.views.map((view) => ({ ...view, motions: [] })) })}>Clear all suggestions</button><SetupChoices removableDirections setup={setup} onChange={setSetup} /></>}
      {step === 4 && <><p>{setup.views.reduce((count, view) => count + view.motions.length, 0)} animations requested. Generation is a separate action in your workspace.</p>{artworkNeeded(setup, facing) > 0 && <p className="setup-note" role="note">{setup.views.some((view) => view.heading === facing) && 'Only your uploaded direction can generate straight away. '}Upload artwork for {artworkNeeded(setup, facing) === 1 ? '1 more direction' : `${artworkNeeded(setup, facing)} more directions`} before their animations can generate.</p>}{artworkNeeded(setup, facing) > 0 && template !== 'single' && <GenerateDirections reason={turnaroundReason(template, facing, setup, machines)} busy={busy} onGenerate={() => void create(true)} />}<div className="setup-summary">{setup.views.map((view) => <div key={view.view_id}><strong>{view.name}</strong><span>{view.motions.map((motion) => motion.name).join(', ') || 'No animations yet'}</span><small>{view.heading === facing ? 'Uses your uploaded artwork' : view.motions.length ? 'Reference needed before generating' : 'Optional direction · no reference needed yet'}</small></div>)}</div><p className="setup-note">{!setup.views.some((view) => view.heading === facing) && 'The supplied direction was removed; its art is retained only as your identity anchor. '}Use this image as the approved character identity {setup.views.some((view) => view.heading === facing) ? 'and the opening reference for ' : 'with its originally declared facing: '}{template === 'single' ? 'Original view' : headings(template).find((item) => item.heading === facing)?.label}. Replacing a direction’s art later keeps this identity anchor.</p></>}
      {error && <p role="alert">{error}</p>}{plan && busy && <p role="status">Saving your reviewed setup…</p>}
      <footer className="setup-actions">{step ? <button className="button" disabled={busy} onClick={() => setStep(step - 1)}>Back</button> : onCancel && <button className="button" onClick={onCancel}>Cancel</button>}<button className="button button--primary" disabled={busy || !file || !name.trim() || (step === 2 && template !== 'single' && !facing)} onClick={() => step === 4 ? void create() : setStep(step + 1)}>{busy ? 'Creating workspace…' : step === 4 ? 'Create workspace' : 'Continue'}</button></footer>
    </section></div></main>
}

/** Why the other directions cannot be turned from this artwork, or null when they can. */
function turnaroundReason(template: Template, facing: Heading | null, setup: ProjectSetup, machines: GenerationConfig | null | undefined): string | null {
  if (template === 'side') return 'Turnarounds currently need a front or three-quarter source'
  if (facing !== 'S' && facing !== 'SE' && facing !== 'SW') return 'Needs front or three-quarter artwork (South, Southeast or Southwest), or upload the other directions yourself'
  // The artwork becomes that direction's reference only if the direction is kept.
  if (!setup.views.some((view) => view.heading === facing)) return "Your artwork's direction was removed, so there is nothing to turn: keep that direction, or upload the other directions yourself"
  if (machines === undefined) return 'Checking for a machine that can turn your artwork…'
  return canStartTurnaround(machines ?? undefined) ? null : 'No machine supports turnarounds yet'
}

function GenerateDirections({ reason, busy, onGenerate }: { reason: string | null; busy: boolean; onGenerate: () => void }) {
  return <div className="setup-note setup-generate-directions">
    <button className="button" disabled={busy || reason !== null} onClick={onGenerate}>Generate the other directions</button>
    <small>{reason ?? 'Creates the workspace, then turns your artwork in place. You pick and check each direction before it is used.'}</small>
  </div>
}
