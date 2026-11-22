import { useSyncExternalStore, type MouseEvent } from 'react'

const navigationEvent = 'rag:workspace-navigation'
const snapshot = () => location.pathname + location.search + location.hash
function subscribe(onChange: () => void) {
  addEventListener('popstate', onChange)
  addEventListener(navigationEvent, onChange)
  return () => {
    removeEventListener('popstate', onChange)
    removeEventListener(navigationEvent, onChange)
  }
}
export function useWorkspaceLocation() {
  return useSyncExternalStore(subscribe, snapshot)
}
export function navigateWorkspace(href: string, replace = false) {
  const url = new URL(href, location.href)
  if (url.origin !== location.origin) throw new Error('Ожидался локальный адрес рабочего места.')
  if (url.href === location.href) return
  if (replace) history.replaceState(null, '', url)
  else history.pushState(null, '', url)
  dispatchEvent(new Event(navigationEvent))
}
// Keep real links: modified clicks, new tabs and file downloads retain browser behavior.
export function followWorkspaceLink(event: MouseEvent<HTMLElement>) {
  if (event.defaultPrevented || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return
  const link = event.target instanceof Element ? event.target.closest<HTMLAnchorElement>('a[href]') : null
  if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return
  const url = new URL(link.href, location.href)
  if (url.origin !== location.origin || url.pathname !== '/' || url.searchParams.get('lab') !== '1') return
  event.preventDefault()
  link.closest<HTMLDialogElement>('dialog[open]')?.close()
  navigateWorkspace(url.href)
}
