// One delegated effect for native controls in both the static and React clients.
// The overlay lives outside React's DOM and cannot change button layout or input.
const reducedMotion = matchMedia('(prefers-reduced-motion: reduce)');
const activeRipples = new Set();
function clearRipples() {
  for (const cleanup of activeRipples) cleanup();
}
function ripple(event) {
  if (reducedMotion.matches || event.defaultPrevented) return;
  const control = event.target instanceof Element
    ? event.target.closest('button, a.link, [role="button"]') : null;
  if (!control || control.matches(':disabled, [aria-disabled="true"]') || control.closest('[inert]')) return;
  if (event.type === 'pointerdown' && (event.button !== 0 || !event.isPrimary)) return;
  if (event.type === 'click' && event.detail !== 0) return; // Pointer already handled.
  const rect = control.getBoundingClientRect();
  if (!rect.width || !rect.height) return;
  const keyboard = event.type === 'click';
  const x = keyboard ? rect.width / 2 : event.clientX - rect.left;
  const y = keyboard ? rect.height / 2 : event.clientY - rect.top;
  const diameter = Math.hypot(Math.max(x, rect.width - x), Math.max(y, rect.height - y)) * 2;
  const layer = document.createElement('span');
  layer.className = 'quiet-ripple-layer';
  layer.setAttribute('aria-hidden', 'true');
  Object.assign(layer.style, {
    left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`,
    borderRadius: getComputedStyle(control).borderRadius,
  });
  const wave = document.createElement('span');
  wave.className = 'quiet-ripple-wave';
  Object.assign(wave.style, {
    width: `${diameter}px`, height: `${diameter}px`,
    left: `${x - diameter / 2}px`, top: `${y - diameter / 2}px`,
  });
  layer.append(wave);
  (control.closest('dialog[open]') || document.body).append(layer);
  const animation = wave.animate([
    { transform: 'scale(.02)', opacity: .65, offset: 0 },
    { transform: 'scale(.7)', opacity: .3, offset: .7 },
    { transform: 'scale(1)', opacity: 0, offset: 1 },
  ], { duration: 720, easing: 'cubic-bezier(.2,0,.4,1)' });
  const cleanup = () => { animation.cancel(); layer.remove(); activeRipples.delete(cleanup); };
  if (activeRipples.size >= 4) activeRipples.values().next().value();
  activeRipples.add(cleanup);
  animation.finished.then(cleanup, cleanup);
}
document.addEventListener('pointerdown', ripple, { passive: true });
document.addEventListener('click', ripple, { passive: true });
addEventListener('scroll', clearRipples, { passive: true, capture: true });
addEventListener('resize', clearRipples, { passive: true });
reducedMotion.addEventListener('change', clearRipples);
document.addEventListener('visibilitychange', () => { if (document.hidden) clearRipples(); });
