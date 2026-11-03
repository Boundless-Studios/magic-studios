export interface SubjectDraft { references: File[]; direction: string }
const drafts = new Map<string, SubjectDraft>()
export function readSubjectDraft(projectId: string): SubjectDraft { return drafts.get(projectId) ?? { references: [], direction: '' } }
export function saveSubjectDraft(projectId: string, draft: SubjectDraft) { drafts.set(projectId, draft) }
export function clearSubjectDraft(projectId: string) { drafts.delete(projectId) }
