// Decorative only. Sleep when settled; never intercept the application's input.
const canvas = document.createElement('canvas');
canvas.className = 'living-dots';
canvas.setAttribute('aria-hidden', 'true');
document.body.prepend(canvas);
const ctx = canvas.getContext('2d');
if (ctx) {
  const motion = matchMedia('(prefers-reduced-motion: reduce)');
  let width = 0, height = 0, frame = 0;
  let pointer = null, strength = 0;
  function draw() {
    frame = 0;
    const target = pointer && !motion.matches && !document.hidden ? 1 : 0;
    strength += (target - strength) * .15;
    if (Math.abs(target - strength) < .005) strength = target;
    ctx.clearRect(0, 0, width, height);
    if (pointer && strength > 0) {
      const glow = ctx.createRadialGradient(pointer.x, pointer.y, 0, pointer.x, pointer.y, 190);
      glow.addColorStop(0, `rgba(40,96,223,${.045 * strength})`);
      glow.addColorStop(1, 'rgba(40,96,223,0)');
      ctx.fillStyle = glow;
      ctx.fillRect(0, 0, width, height);
    }
    for (let y = 14; y < height; y += 28) {
      for (let x = 14; x < width; x += 28) {
        const dx = pointer ? pointer.x - x : 0;
        const dy = pointer ? pointer.y - y : 0;
        const distance = Math.hypot(dx, dy);
        const influence = pointer ? Math.max(0, 1 - distance / 160) ** 2 * strength : 0;
        const pull = influence * 6 / Math.max(distance, 1);
        ctx.fillStyle = influence > .08 ? `rgba(40,96,223,${.23 + influence * .2})` : '#cbd2dc';
        ctx.beginPath();
        ctx.arc(x + dx * pull, y + dy * pull, .65 + influence * .35, 0, Math.PI * 2);
        ctx.fill();
      }
    }
    if (strength !== target) schedule();
  }
  function schedule() { if (!frame && !document.hidden) frame = requestAnimationFrame(draw); }
  function resize() {
    width = innerWidth; height = innerHeight;
    const scale = Math.min(devicePixelRatio || 1, 2);
    canvas.width = width * scale; canvas.height = height * scale;
    ctx.setTransform(scale, 0, 0, scale, 0, 0);
    schedule();
  }
  addEventListener('pointermove', event => {
    if (event.pointerType === 'touch' || motion.matches) return;
    pointer = { x: event.clientX, y: event.clientY }; schedule();
  }, { passive: true });
  document.documentElement.addEventListener('pointerleave', () => { pointer = null; schedule(); });
  addEventListener('blur', () => { pointer = null; schedule(); });
  addEventListener('resize', resize, { passive: true });
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) { cancelAnimationFrame(frame); frame = 0; pointer = null; }
    else schedule();
  });
  motion.addEventListener('change', () => { pointer = null; strength = 0; schedule(); });
  resize();
}
