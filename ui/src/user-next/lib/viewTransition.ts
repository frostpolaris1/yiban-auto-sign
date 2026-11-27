/**
 * Native same-document view transitions for the user-next layer.
 *
 * The browser owns the snapshot; React/TanStack only own the DOM update inside
 * the transition callback. When the API is unavailable, or the user asks for
 * reduced motion, the navigation runs immediately with no visual fork.
 *
 * We deliberately do not use TanStack Router's `viewTransition` option here:
 * it starts its own transition and would nest with this one. Navigation calls
 * in user-next pass plain options and wrap them with `withViewTransition`.
 */

type ViewTransitionLike = {
  finished: Promise<void>
  ready: Promise<void>
  updateCallbackDone: Promise<void>
  skipTransition: () => void
}

type ViewTransitionDocument = Document & {
  startViewTransition?: (callback: () => void | Promise<void>) => ViewTransitionLike
}

let active = false

export function canViewTransition(): boolean {
  if (typeof document === 'undefined' || typeof window === 'undefined') return false
  const doc = document as ViewTransitionDocument
  if (typeof doc.startViewTransition !== 'function') return false
  return !window.matchMedia('(prefers-reduced-motion: reduce)').matches
}

function nextPaint(): Promise<void> {
  return new Promise((resolve) => {
    requestAnimationFrame(() => requestAnimationFrame(() => resolve()))
  })
}

/**
 * Run `update` inside a native view transition and tag the shared elements
 * named by the CSS contract:
 *
 *   html[data-un-vt="result"] .un-shared-result { view-transition-name: ... }
 *
 * The data attribute is removed after `finished`, including on failure. If a
 * transition is already running, the new update is applied without a second
 * snapshot so two transitions can never race.
 */
export function withViewTransition(
  name: string,
  update: () => void | Promise<void>,
  source?: HTMLElement | null,
): void {
  if (!canViewTransition() || active) {
    void update()
    return
  }

  const doc = document as ViewTransitionDocument
  const root = document.documentElement
  active = true
  root.dataset.unVt = name

  // The destination opts in through the CSS contract; the source is usually a
  // repeated item (a day cell) that cannot carry a permanent name without
  // colliding with its siblings, so name it only for this transition.
  if (source) source.style.viewTransitionName = `un-${name}`

  let transition: ViewTransitionLike
  try {
    transition = doc.startViewTransition!(async () => {
      await update()
      await nextPaint()
    })
  } catch {
    active = false
    delete root.dataset.unVt
    if (source) source.style.viewTransitionName = ''
    void update()
    return
  }

  void transition.finished
    .catch(() => undefined)
    .finally(() => {
      active = false
      delete root.dataset.unVt
      if (source) source.style.viewTransitionName = ''
    })
}
