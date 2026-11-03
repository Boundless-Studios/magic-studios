import type { SubjectFinding } from '../../api/types'

/** What the finding's button does: a server-side pixel fix, or open the correction form. */
export type SubjectFindingActionKind = 'reframe' | 'cleanup' | 'correct'

export interface SubjectFindingAdvice {
  /** One plain sentence, no code name, no policy vocabulary. */
  sentence: string
  actionLabel: string
  action: SubjectFindingActionKind
  /** Prefill for "Correct with feedback" -- always a human instruction. */
  correctionPrefill: string
}

const FIT_TO_FRAME = 'Fit to frame'
const CLEAN_UP = 'Clean up'

/**
 * Operator feedback 2026-09-06 ("i dont know what to do with this warning"): every subject
 * finding must read as a sentence about the picture and carry a button that fixes it. A finding
 * code never reaches the screen, and never prefills the correction box.
 */
export function subjectFindingAdvice(finding: SubjectFinding): SubjectFindingAdvice {
  switch (finding.code) {
    case 'subject_occupancy_too_low':
      return { sentence: 'The character is small in the frame.', actionLabel: FIT_TO_FRAME, action: 'reframe', correctionPrefill: 'Make the character fill more of the frame.' }
    case 'subject_touches_canvas_edge':
      return { sentence: 'The character touches the edge of the frame.', actionLabel: FIT_TO_FRAME, action: 'reframe', correctionPrefill: 'Leave a clear margin around the character.' }
    // Framing already scales the character to 90% of the canvas, so anything above the ceiling is
    // extra opaque area around it -- exactly what the cleanup pass removes.
    case 'subject_occupancy_too_high':
      return { sentence: 'There are stray fragments around the character.', actionLabel: CLEAN_UP, action: 'cleanup', correctionPrefill: 'Remove everything except the character itself.' }
    case 'subject_missing_alpha':
      return { sentence: 'The background has not been removed.', actionLabel: 'Correct with feedback', action: 'correct', correctionPrefill: 'Remove the background completely so only the character remains.' }
    case 'subject_dimension_too_small':
      return { sentence: 'The picture is too small to animate well.', actionLabel: 'Correct with feedback', action: 'correct', correctionPrefill: 'Draw the character larger and sharper.' }
    default:
      return { sentence: finding.explanation, actionLabel: 'Correct with feedback', action: 'correct', correctionPrefill: finding.explanation }
  }
}
