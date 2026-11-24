// Sparx's orb: a 3D particle sphere on a canvas. Plain JavaScript, no libraries, nothing loaded from outside this server.
// It follows <body data-state="...">, which app.js already sets (offline, stopped, idle, listening, thinking, speaking),
// and takes its colour from the --hud CSS variable. app.js can nudge it with SparxOrb.pulse() when speech is heard.
'use strict';
(() => {
  const canvas = document.getElementById('orb');
  if (!canvas || !canvas.getContext) return;
  const ctx = canvas.getContext('2d');
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');

  const N = 620;                     // particles
  const LINKS = 3;                   // nearest neighbours each particle is joined to
  const TAU = Math.PI * 2;

  // Fibonacci sphere: evenly spread points, so the surface looks like a mesh rather than clumps
  const base = new Float32Array(N * 3);
  const seed = new Float32Array(N * 3);
  for (let i = 0; i < N; i++) {
    const y = 1 - (2 * (i + 0.5)) / N, r = Math.sqrt(1 - y * y), a = i * Math.PI * (3 - Math.sqrt(5));
    base.set([Math.cos(a) * r, y, Math.sin(a) * r], i * 3);
    seed.set([Math.random() * TAU, Math.random() * TAU, Math.random() * TAU], i * 3);
  }
  const links = [];
  {
    const seen = new Set();
    for (let i = 0; i < N; i++) {
      const d = [];
      for (let j = 0; j < N; j++) {
        if (j === i) continue;
        const dx = base[i * 3] - base[j * 3], dy = base[i * 3 + 1] - base[j * 3 + 1], dz = base[i * 3 + 2] - base[j * 3 + 2];
        d.push([dx * dx + dy * dy + dz * dz, j]);
      }
      d.sort((p, q) => p[0] - q[0]);
      for (let k = 0; k < LINKS; k++) {
        const j = d[k][1], key = i < j ? i * N + j : j * N + i;
        if (!seen.has(key)) { seen.add(key); links.push(i, j); }
      }
    }
  }

  // per-state targets; the live values glide toward these so a state change reads as motion, not a cut
  const MODES = {
    offline:   { spin: 0.04, amp: 0.012, wave: 0.4, size: 0.86, glow: 0.25, ring: 0,   link: 0.10, speed: 0.4 },
    stopped:   { spin: 0.00, amp: 0.006, wave: 0.2, size: 0.86, glow: 0.35, ring: 0,   link: 0.14, speed: 0.2 },
    idle:      { spin: 0.18, amp: 0.035, wave: 0.9, size: 0.90, glow: 0.55, ring: 0.3, link: 0.20, speed: 0.8 },
    listening: { spin: 0.30, amp: 0.060, wave: 1.4, size: 0.95, glow: 0.80, ring: 0.6, link: 0.26, speed: 1.2 },
    thinking:  { spin: 1.10, amp: 0.110, wave: 3.0, size: 0.88, glow: 0.95, ring: 1.0, link: 0.34, speed: 2.4 },
    speaking:  { spin: 0.42, amp: 0.090, wave: 2.0, size: 0.93, glow: 0.90, ring: 0.5, link: 0.28, speed: 1.6 },
  };
  const live = { ...MODES.offline };
  let mode = 'offline';

  let rgb = [63, 214, 255], size = 0, dpr = 1, running = false, last = 0, t = 0;
  let rotY = 0.6, rotX = 0.35, velY = 0, velX = 0, level = 0, drag = null, hover = { x: 0, y: 0 };
  const ripples = [];

  /** Read --hud as r,g,b by letting the canvas parse whatever CSS colour it is. */
  function readColour() {
    const c = getComputedStyle(document.body).getPropertyValue('--hud').trim() || '#3fd6ff';
    ctx.fillStyle = '#000';
    ctx.fillStyle = c;
    const v = ctx.fillStyle;
    if (v[0] === '#') rgb = [1, 3, 5].map((i) => parseInt(v.slice(i, i + 2), 16));
    else { const m = /(\d+)\D+(\d+)\D+(\d+)/.exec(v); if (m) rgb = [+m[1], +m[2], +m[3]]; }
  }
  const col = (a) => `rgba(${rgb[0]},${rgb[1]},${rgb[2]},${Math.max(0, Math.min(1, a)).toFixed(3)})`;

  function fit() {
    const r = canvas.getBoundingClientRect();
    dpr = Math.min(window.devicePixelRatio || 1, 2);
    size = Math.round(Math.min(r.width, r.height));
    if (!size) return false;
    if (canvas.width !== size * dpr || canvas.height !== size * dpr) { canvas.width = canvas.height = size * dpr; }
    return true;
  }

  function setMode() {
    const s = document.body.dataset.state;
    mode = MODES[s] ? s : 'idle';
    readColour();
  }

  // value noise from a few sines: cheap, smooth, different for every particle
  function wobble(i, tt) {
    const a = seed[i * 3], b = seed[i * 3 + 1], c = seed[i * 3 + 2];
    return Math.sin(tt * 1.3 + a) * 0.5 + Math.sin(tt * 2.1 + b + base[i * 3 + 1] * 4) * 0.3 + Math.sin(tt * 0.7 + c + base[i * 3] * 3) * 0.2;
  }

  const proj = new Float32Array(N * 4);       // x, y, depth (0 far .. 1 near), alpha

  function frame(now) {
    if (!running) return;
    requestAnimationFrame(frame);
    const dt = Math.min(0.05, (now - last) / 1000 || 0.016);
    last = now;
    if (!canvas.offsetParent || document.hidden || !fit()) return;       // not on screen: skip the work
    const calm = reduced.matches ? 0.25 : 1;

    const goal = MODES[mode], k = 1 - Math.exp(-dt * 3);
    for (const key in goal) live[key] += (goal[key] - live[key]) * k;
    t += dt * live.speed * calm;

    // the energy of speech: while speaking it is a made-up envelope (the browser gives no audio data for synthesis),
    // while listening it follows pulse() calls from the recogniser
    if (mode === 'speaking') level += ((0.35 + 0.65 * Math.abs(Math.sin(t * 5.3) * Math.sin(t * 2.1 + 1)) * (0.6 + 0.4 * Math.sin(t * 11))) - level) * Math.min(1, dt * 14);
    else level *= Math.exp(-dt * 3.5);
    const amp = live.amp * (1 + level * (mode === 'listening' ? 3.2 : 1.6));

    // rotation: constant spin plus whatever the pointer gave it
    if (!drag) { rotY += (live.spin * calm + velY) * dt; rotX += velX * dt; velY *= Math.exp(-dt * 2.2); velX *= Math.exp(-dt * 2.2); }
    rotX += ((0.35 + hover.y * 0.25) - rotX) * Math.min(1, dt * 0.8) * (drag ? 0 : 1);
    const cy = Math.cos(rotY), sy = Math.sin(rotY), cx = Math.cos(rotX), sx = Math.sin(rotX);

    const R = size * 0.5 * 0.62 * live.size * (1 + Math.sin(t * 1.1) * 0.012 + level * 0.05);
    const cxp = size * 0.5, cyp = size * 0.5, cam = 3.2;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, size, size);

    // glow behind everything
    const g = ctx.createRadialGradient(cxp, cyp, R * 0.1, cxp, cyp, R * 1.9);
    g.addColorStop(0, col(0.30 * live.glow + level * 0.2));
    g.addColorStop(0.45, col(0.10 * live.glow));
    g.addColorStop(1, col(0));
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, size, size);

    // particles
    for (let i = 0; i < N; i++) {
      const w = 1 + wobble(i, t * live.wave) * amp;
      let x = base[i * 3] * w, y = base[i * 3 + 1] * w, z = base[i * 3 + 2] * w;
      let x1 = x * cy + z * sy, z1 = -x * sy + z * cy;           // spin about the vertical axis
      let y1 = y * cx - z1 * sx, z2 = y * sx + z1 * cx;          // tilt toward the viewer
      const s = cam / (cam - z2);
      proj[i * 4] = cxp + x1 * R * s;
      proj[i * 4 + 1] = cyp + y1 * R * s;
      const depth = (z2 + 1.3) / 2.6;
      proj[i * 4 + 2] = depth;
      proj[i * 4 + 3] = s;
    }

    // links, drawn in three depth bands so far lines are fainter than near ones
    ctx.lineWidth = 0.8;
    for (let band = 0; band < 3; band++) {
      ctx.beginPath();
      for (let n = 0; n < links.length; n += 2) {
        const a = links[n], b = links[n + 1];
        const d = (proj[a * 4 + 2] + proj[b * 4 + 2]) * 0.5;
        if ((d < 0.4 ? 0 : d < 0.65 ? 1 : 2) !== band) continue;
        ctx.moveTo(proj[a * 4], proj[a * 4 + 1]);
        ctx.lineTo(proj[b * 4], proj[b * 4 + 1]);
      }
      ctx.strokeStyle = col(live.link * (0.35 + band * 0.4) * (1 + level * 0.8));
      ctx.stroke();
    }

    // points, brighter and bigger as they come toward the viewer
    for (let i = 0; i < N; i++) {
      const d = proj[i * 4 + 2], s = proj[i * 4 + 3];
      const tw = 0.75 + 0.25 * Math.sin(t * 3 + seed[i * 3]);
      const r = (0.7 + d * 1.5) * s * (size / 260) * (1 + level * 0.5);
      ctx.fillStyle = col((0.12 + d * 0.88) * tw);
      ctx.beginPath();
      ctx.arc(proj[i * 4], proj[i * 4 + 1], Math.max(0.4, r), 0, TAU);
      ctx.fill();
    }

    // bright core
    const core = ctx.createRadialGradient(cxp, cyp, 0, cxp, cyp, R * 0.55);
    core.addColorStop(0, col(0.55 * live.glow + level * 0.3));
    core.addColorStop(1, col(0));
    ctx.fillStyle = core;
    ctx.beginPath();
    ctx.arc(cxp, cyp, R * 0.55, 0, TAU);
    ctx.fill();

    // orbiting arcs, tilted differently; they speed up with the rest of the orb
    if (live.ring > 0.02) {
      ctx.lineCap = 'round';
      for (let n = 0; n < 3; n++) {
        const tilt = 1.1 + n * 0.55, a0 = t * (0.9 + n * 0.45) * (n % 2 ? -1 : 1) + n * 2, sweep = 0.9 + 0.5 * Math.sin(t * 0.8 + n);
        ctx.save();
        ctx.translate(cxp, cyp);
        ctx.rotate(tilt);
        ctx.scale(1, 0.34 + 0.1 * n);
        ctx.beginPath();
        ctx.arc(0, 0, R * (1.22 + n * 0.1), a0, a0 + sweep);
        ctx.restore();
        ctx.strokeStyle = col(0.55 * live.ring);
        ctx.lineWidth = 1.6 - n * 0.3;
        ctx.stroke();
      }
    }

    // ripples leaving the sphere while it listens
    if (mode === 'listening' && (ripples.length === 0 || t - ripples[ripples.length - 1] > 1.3 / live.speed)) ripples.push(t);
    for (let n = ripples.length - 1; n >= 0; n--) {
      const age = (t - ripples[n]) * 0.55;
      if (age > 1) { ripples.splice(n, 1); continue; }
      ctx.beginPath();
      ctx.arc(cxp, cyp, R * (1.02 + age * 0.6), 0, TAU);
      ctx.strokeStyle = col((1 - age) * 0.45);
      ctx.lineWidth = 1.4;
      ctx.stroke();
    }
  }

  // drag to spin; the flick carries on and fades. Taps still reach the button underneath (app.js stops speech on click).
  canvas.addEventListener('pointerdown', (e) => {
    drag = { x: e.clientX, y: e.clientY, moved: 0 };
    canvas.setPointerCapture(e.pointerId);
  });
  canvas.addEventListener('pointermove', (e) => {
    const r = canvas.getBoundingClientRect();
    hover.x = ((e.clientX - r.left) / r.width - 0.5) * 2;
    hover.y = ((e.clientY - r.top) / r.height - 0.5) * 2;
    if (!drag) return;
    const dx = e.clientX - drag.x, dy = e.clientY - drag.y;
    drag.x = e.clientX; drag.y = e.clientY; drag.moved += Math.abs(dx) + Math.abs(dy);
    rotY += dx * 0.012; rotX = Math.max(-1.2, Math.min(1.2, rotX + dy * 0.012));
    velY = dx * 0.4; velX = dy * 0.2;
  });
  const release = () => { drag = null; };
  canvas.addEventListener('pointerup', release);
  canvas.addEventListener('pointercancel', release);
  canvas.addEventListener('pointerleave', () => { hover.x = hover.y = 0; });

  new MutationObserver(setMode).observe(document.body, { attributes: true, attributeFilter: ['data-state'] });
  matchMedia('(prefers-color-scheme: light)').addEventListener('change', readColour);
  window.addEventListener('resize', fit);

  function start() {
    if (running) return;
    running = true;
    last = performance.now();
    requestAnimationFrame(frame);
  }
  setMode();
  start();

  window.SparxOrb = {
    /** A burst of energy, 0..1: the recogniser heard something. */
    pulse(v) { level = Math.max(level, Math.min(1, v == null ? 0.7 : v)); },
  };
})();
