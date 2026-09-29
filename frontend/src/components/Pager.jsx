import React from 'react'

// ---------------------------------------------------------------------------
// The pager under a paged table.
//
// Shared because the user's job history and the administrator's two job lists
// are the same problem: a list too long to send whole, polled every few
// seconds, where the reader has to keep their place while it changes
// underneath them.
// ---------------------------------------------------------------------------

const GAP = Symbol('gap')

/** Page numbers to show: the two ends, and a window around where you are. */
export function pageNumbers(current, total, window = 1) {
  const wanted = new Set([1, total])
  for (let n = current - window; n <= current + window; n += 1) {
    if (n >= 1 && n <= total) wanted.add(n)
  }
  const sorted = [...wanted].sort((a, b) => a - b)
  const out = []
  sorted.forEach((n, i) => {
    if (i > 0 && n - sorted[i - 1] > 1) out.push(GAP)
    out.push(n)
  })
  return out
}

function PageButton({ onClick, disabled, label, children }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={label}
      className="px-2 py-1 rounded-md text-xs font-medium text-gray-600 hover:bg-gray-100 disabled:opacity-40 disabled:hover:bg-transparent"
    >
      {children}
    </button>
  )
}

/**
 * `meta` is the server's pagination block ({page, per_page, total, pages}).
 * Renders nothing at all while everything fits on one page, because a pager
 * under four rows is furniture.
 */
export default function Pager({ meta, onPage, busy = false, shown = 0 }) {
  const pages = Math.max(1, meta?.pages ?? 1)
  if (pages <= 1) return null

  const page = meta.page ?? 1
  const first = meta.total ? (page - 1) * meta.per_page + 1 : 0
  const last = meta.total ? first + shown - 1 : 0

  return (
    <div className="px-5 py-3 border-t border-gray-100 flex items-center justify-between">
      <p className="text-[11px] text-gray-500">{first}–{last} of {meta.total}</p>
      <div className="flex items-center space-x-1">
        <PageButton onClick={() => onPage(page - 1)}
                    disabled={page <= 1 || busy} label="Previous">‹</PageButton>
        {pageNumbers(page, pages).map((n, i) => (
          n === GAP ? (
            <span key={`gap-${i}`} className="px-1 text-xs text-gray-400">…</span>
          ) : (
            <button
              key={n}
              onClick={() => onPage(n)}
              disabled={busy}
              aria-current={n === page ? 'page' : undefined}
              className={`min-w-[1.85rem] px-2 py-1 rounded-md text-xs font-medium transition-colors ${
                n === page
                  ? 'bg-indigo-600 text-white'
                  : 'text-gray-600 hover:bg-gray-100 disabled:opacity-50'
              }`}
            >
              {n}
            </button>
          )
        ))}
        <PageButton onClick={() => onPage(page + 1)}
                    disabled={page >= pages || busy} label="Next">›</PageButton>
      </div>
    </div>
  )
}
