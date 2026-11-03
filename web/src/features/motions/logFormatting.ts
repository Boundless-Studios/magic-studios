/** Shared `Intl.DateTimeFormat` instances for the activity logs. `Date.prototype.toLocaleString`
 * builds a formatter per call, which is the single most expensive thing a 20-row log does; the
 * output is identical to the `toLocaleString([], options)` calls these replace. */
const ENTRY_FORMAT = new Intl.DateTimeFormat(undefined, { dateStyle: 'medium', timeStyle: 'short' })
const SHORT_TIME_FORMAT = new Intl.DateTimeFormat(undefined, { hour: 'numeric', minute: '2-digit' })

/** A log entry's full timestamp ("12 Mar 2026, 14:07"). */
export function entryTimestamp(isoTimestamp: string): string {
  return ENTRY_FORMAT.format(new Date(isoTimestamp))
}

/** The summary line's "last H:MM". */
export function shortTime(isoTimestamp: string): string {
  return SHORT_TIME_FORMAT.format(new Date(isoTimestamp))
}
