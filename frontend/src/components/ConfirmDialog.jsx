import React, { useCallback, useEffect, useRef, useState } from 'react'

// ---------------------------------------------------------------------------
// Confirmation for the actions that cannot be taken back.
//
// The browser's own confirm() blocks the page, cannot be styled, and says
// "127.0.0.1:7080 says" above whatever you wrote, which reads like a scam.
// It also cannot show the one thing that makes a confirmation useful, which is
// what exactly is about to happen: which job, whose workspace, what survives.
//
// Used through `useConfirm()`, so a call site reads as a question:
//
//     const [dialog, confirm] = useConfirm()
//     if (!await confirm({ title: 'Cancel job 12?', ... })) return
//     …
//     return <>{dialog} …</>
// ---------------------------------------------------------------------------

function ConfirmDialog({
  title,
  body,
  detail,
  confirmLabel = 'Confirm',
  cancelLabel = 'Go back',
  tone = 'danger',
  onConfirm,
  onCancel,
}) {
  // The safe choice takes the focus, so a stray Enter closes the dialog
  // rather than going through with it.
  const cancelRef = useRef(null)
  useEffect(() => { cancelRef.current?.focus() }, [])

  useEffect(() => {
    const onKey = (e) => { if (e.key === 'Escape') onCancel() }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [onCancel])

  const confirmTone = tone === 'danger'
    ? 'bg-red-600 hover:bg-red-700 shadow-red-200'
    : 'bg-indigo-600 hover:bg-indigo-700 shadow-indigo-200'

  return (
    <div
      className="fixed inset-0 z-[60] bg-black/40 backdrop-blur-sm flex items-center justify-center p-4"
      onClick={onCancel}
      role="dialog"
      aria-modal="true"
    >
      <div
        className="bg-white rounded-2xl shadow-2xl w-full max-w-md p-6"
        onClick={(e) => e.stopPropagation()}
      >
        <h3 className="text-base font-semibold text-gray-900">{title}</h3>
        {body && <p className="text-sm text-gray-600 mt-2 leading-relaxed">{body}</p>}
        {detail?.length > 0 && (
          <ul className="text-xs text-gray-500 space-y-1 mt-3 list-disc pl-5">
            {detail.map((line, i) => <li key={i}>{line}</li>)}
          </ul>
        )}
        <div className="flex justify-end space-x-2 mt-5">
          <button
            ref={cancelRef}
            onClick={onCancel}
            className="px-4 py-2 text-sm font-medium text-gray-700 bg-gray-100 hover:bg-gray-200 rounded-lg transition-colors"
          >
            {cancelLabel}
          </button>
          <button
            onClick={onConfirm}
            className={`px-4 py-2 text-sm font-semibold text-white rounded-lg shadow-sm transition-colors ${confirmTone}`}
          >
            {confirmLabel}
          </button>
        </div>
      </div>
    </div>
  )
}

/**
 * Returns `[dialog, confirm]`. Render `dialog`; await `confirm(options)`,
 * which resolves true when the person went through with it.
 */
export function useConfirm() {
  const [pending, setPending] = useState(null)

  const confirm = useCallback(
    (options) => new Promise((resolve) => setPending({ options, resolve })),
    [],
  )

  const answer = (value) => {
    pending?.resolve(value)
    setPending(null)
  }

  const dialog = pending ? (
    <ConfirmDialog
      {...pending.options}
      onConfirm={() => answer(true)}
      onCancel={() => answer(false)}
    />
  ) : null

  return [dialog, confirm]
}

export default ConfirmDialog
