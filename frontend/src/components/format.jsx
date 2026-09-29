import React from 'react'

// ---------------------------------------------------------------------------
// Formatting shared by the job tables, the user's and the administrator's.
// Kept in one place because the timestamp handling below is easy to get wrong
// in a way nobody notices until the hours look plausible but are not.
// ---------------------------------------------------------------------------

// The API sends naive UTC timestamps. Without the Z a browser reads them as
// local time, which puts every job seven hours out in Hanoi.
export function asDate(iso) {
  if (!iso) return null
  const d = new Date(/[Zz]|[+-]\d\d:?\d\d$/.test(iso) ? iso : `${iso}Z`)
  return Number.isNaN(d.getTime()) ? null : d
}

// Short enough for a table cell, with the whole thing on hover.
export function stamp(iso) {
  const d = asDate(iso)
  if (!d) return { short: '—', full: '' }
  const today = new Date()
  const sameDay = d.toDateString() === today.toDateString()
  return {
    short: sameDay
      ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
      : d.toLocaleString([], { day: '2-digit', month: 'short',
                               hour: '2-digit', minute: '2-digit' }),
    full: d.toLocaleString(),
  }
}

export function TimeCell({ iso }) {
  const { short, full } = stamp(iso)
  return (
    <span title={full} className="font-mono text-xs text-gray-600 whitespace-nowrap">
      {short}
    </span>
  )
}

export function duration(seconds) {
  if (seconds == null) return '—'
  if (seconds < 60) return `${seconds}s`
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ${seconds % 60}s`
  return `${Math.floor(seconds / 3600)}h ${Math.floor((seconds % 3600) / 60)}m`
}
