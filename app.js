(function () {
  "use strict";

  const DATA_URL = "./data/reply_clock.json";
  const DURATION_MS = 18000;
  const HOLD_MS = 1800;
  const FADE_MS = 1000;
  const SPAWN_FRAC = 0.84;
  const TWO_PI = Math.PI * 2;

  const canvas = document.getElementById("clock");
  const ctx = canvas.getContext("2d");
  const btnPlay = document.getElementById("btnPlay");
  const btnReplay = document.getElementById("btnReplay");
  const scrub = document.getElementById("scrub");
  const mCount = document.getElementById("mCount");
  const yrLabel = document.getElementById("yrLabel");
  const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

  const COLOR_STOPS = [
    [0.0, [255, 214, 110]],
    [0.2, [245, 158, 61]],
    [0.42, [232, 99, 60]],
    [0.66, [168, 60, 140]],
    [0.84, [70, 96, 150]],
    [1.0, [48, 30, 72]],
  ];
  const SPRITE_BUCKETS = 36;

  const state = {
    meta: null,
    dots: null,
    sprites: [],
    spriteSize: 18,
    w: 0, h: 0, dpr: 1,
    cx: 0, cy: 0, rIn: 6, rOut: 100,
    logCap: Math.log(10081),
    rotation: 0,
    playing: false,
    playhead: 0,
    started: 0,
    raf: 0,
    totalMs: DURATION_MS + HOLD_MS + FADE_MS,
  };

  const clamp = (v, a, b) => Math.min(b, Math.max(a, v));
  const easeOutCubic = (t) => 1 - Math.pow(1 - clamp(t, 0, 1), 3);

  function lerpColor(t) {
    t = clamp(t, 0, 1);
    for (let i = 0; i < COLOR_STOPS.length - 1; i++) {
      const [a, ca] = COLOR_STOPS[i];
      const [b, cb] = COLOR_STOPS[i + 1];
      if (t <= b) {
        const f = b > a ? (t - a) / (b - a) : 0;
        return [
          Math.round(ca[0] + (cb[0] - ca[0]) * f),
          Math.round(ca[1] + (cb[1] - ca[1]) * f),
          Math.round(ca[2] + (cb[2] - ca[2]) * f),
        ];
      }
    }
    return COLOR_STOPS[COLOR_STOPS.length - 1][1];
  }

  function buildSprites() {
    state.sprites = [];
    const R = state.spriteSize;
    const size = R * 2;
    const RC = R * 0.52;
    const lt = (v) => Math.min(255, Math.round(v + (255 - v) * 0.5));
    const dk = (v) => Math.round(v * 0.46);
    for (let b = 0; b < SPRITE_BUCKETS; b++) {
      const [r, g, bl] = lerpColor(b / (SPRITE_BUCKETS - 1));
      const s = document.createElement("canvas");
      s.width = s.height = size;
      const c = s.getContext("2d");
      const halo = c.createRadialGradient(R, R, RC * 0.6, R, R, R);
      halo.addColorStop(0, `rgba(${r},${g},${bl},0.16)`);
      halo.addColorStop(0.5, `rgba(${r},${g},${bl},0.05)`);
      halo.addColorStop(1, `rgba(${r},${g},${bl},0)`);
      c.fillStyle = halo;
      c.fillRect(0, 0, size, size);
      const hx = R - RC * 0.42, hy = R - RC * 0.42;
      const bead = c.createRadialGradient(hx, hy, RC * 0.1, R, R, RC);
      bead.addColorStop(0, `rgb(${lt(r)},${lt(g)},${lt(bl)})`);
      bead.addColorStop(0.45, `rgb(${r},${g},${bl})`);
      bead.addColorStop(1, `rgb(${dk(r)},${dk(g)},${dk(bl)})`);
      c.beginPath();
      c.arc(R, R, RC, 0, Math.PI * 2);
      c.fillStyle = bead;
      c.fill();
      state.sprites.push(s);
    }
  }

  function tForLatency(lat) {
    return clamp(Math.log(lat + 1) / state.logCap, 0, 1);
  }

  function prepareDots(payload) {
    state.meta = payload.meta;
    state.rotation = (payload.meta.mean_hour / 24) * TWO_PI;
    const rows = payload.rows;
    const maxDay = payload.meta.archive_day_count;
    const n = rows.length;
    const dots = {
      n,
      t: new Float32Array(n),
      angle: new Float32Array(n),
      spawn: new Float32Array(n),
      travel: new Float32Array(n),
      bucket: new Uint8Array(n),
      jitR: new Float32Array(n),
      jitA: new Float32Array(n),
      alpha: new Float32Array(n),
      day: new Uint16Array(n),
    };
    for (let i = 0; i < n; i++) {
      const hour = rows[i][0];
      const lat = rows[i][1];
      const day = rows[i][2];
      const t = tForLatency(lat);
      dots.t[i] = t;
      dots.angle[i] = -Math.PI / 2 + ((hour / 24) * TWO_PI - state.rotation);
      dots.spawn[i] = (day / maxDay) * SPAWN_FRAC;
      dots.travel[i] = 0.012 + t * 0.075;
      dots.bucket[i] = Math.min(SPRITE_BUCKETS - 1, Math.round(t * (SPRITE_BUCKETS - 1)));
      const seed = i * 2654435761;
      dots.jitR[i] = (((seed >>> 13) & 255) / 255 - 0.5);
      dots.jitA[i] = (((seed >>> 7) & 255) / 255 - 0.5);
      dots.alpha[i] = t < 0.5 ? 0.95 : 0.82;
      dots.day[i] = day;
    }
    dots.order = Array.from({ length: n }, (_, i) => i).sort((p, q) => dots.t[q] - dots.t[p]);
    state.dots = dots;
  }

  function resize() {
    const rect = canvas.getBoundingClientRect();
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    state.w = Math.max(1, Math.floor(rect.width));
    state.h = Math.max(1, Math.floor(rect.height));
    canvas.width = Math.floor(state.w * state.dpr);
    canvas.height = Math.floor(state.h * state.dpr);
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
    state.cx = state.w / 2;
    state.cy = state.h * 0.5;
    state.rOut = Math.min(state.w, state.h) * 0.40;
    state.rIn = Math.max(4, state.rOut * 0.03);
    state.spriteSize = clamp(state.rOut * 0.02, 5, 12);
    buildSprites();
    render(state.playhead);
  }

  function radiusOf(t) {
    return state.rIn + t * (state.rOut - state.rIn);
  }

  function haloText(txt, x, y) {
    ctx.lineWidth = 3.5;
    ctx.strokeStyle = "rgba(8,6,14,0.82)";
    ctx.strokeText(txt, x, y);
    ctx.fillText(txt, x, y);
  }

  function drawChrome() {
    const { cx, cy, rOut } = state;
    ctx.save();
    ctx.lineWidth = 1;
    ctx.font = `${clamp(rOut * 0.036, 11, 14)}px Inter, sans-serif`;
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    state.meta.rings.forEach(([lat, label]) => {
      const r = radiusOf(tForLatency(lat));
      ctx.strokeStyle = "rgba(150,146,175,0.20)";
      ctx.beginPath();
      ctx.arc(cx, cy, r, 0, TWO_PI);
      ctx.stroke();
      if (lat === 1) return;
      ctx.fillStyle = "rgba(190,188,204,0.9)";
      haloText(label, cx, cy + r);
    });
    const labels = [["midnight", 0], ["6am", 6], ["noon", 12], ["6pm", 18]];
    labels.forEach(([txt, hour]) => {
      const ang = -Math.PI / 2 + ((hour / 24) * TWO_PI - state.rotation);
      const r0 = rOut + 12, r1 = rOut + 22;
      ctx.strokeStyle = "rgba(150,150,178,0.5)";
      ctx.beginPath();
      ctx.moveTo(cx + r0 * Math.cos(ang), cy + r0 * Math.sin(ang));
      ctx.lineTo(cx + r1 * Math.cos(ang), cy + r1 * Math.sin(ang));
      ctx.stroke();
      ctx.fillStyle = "rgba(200,198,216,0.92)";
      haloText(txt, cx + (rOut + 46) * Math.cos(ang), cy + (rOut + 46) * Math.sin(ang));
    });
    ctx.restore();
  }

  function drawDots(effMs, globalFade) {
    const d = state.dots;
    const { cx, cy, rIn, spriteSize } = state;
    const p = effMs / DURATION_MS;
    const size = spriteSize * 2;
    let shown = 0;
    ctx.save();
    for (let k = 0; k < d.n; k++) {
      const i = d.order[k];
      const sp = d.spawn[i];
      if (p < sp) continue;
      const prog = easeOutCubic((p - sp) / d.travel[i]);
      const rf = radiusOf(d.t[i]) + d.jitR[i] * (state.rOut * 0.012);
      const r = rIn + (rf - rIn) * prog;
      const ang = d.angle[i] + d.jitA[i] * 0.02;
      const x = cx + r * Math.cos(ang);
      const y = cy + r * Math.sin(ang);
      ctx.globalAlpha = d.alpha[i] * (0.5 + 0.5 * prog) * globalFade;
      ctx.drawImage(state.sprites[d.bucket[i]], x - spriteSize, y - spriteSize, size, size);
      shown++;
    }
    ctx.restore();
    return shown;
  }

  function background() {
    ctx.clearRect(0, 0, state.w, state.h);
    const g = ctx.createRadialGradient(state.cx, state.cy, 0, state.cx, state.cy, Math.max(state.w, state.h) * 0.7);
    g.addColorStop(0, "#140f20");
    g.addColorStop(0.55, "#0d0b14");
    g.addColorStop(1, "#07050d");
    ctx.fillStyle = g;
    ctx.fillRect(0, 0, state.w, state.h);
  }

  function yearFor(p) {
    if (!state.meta) return "";
    const day = clamp(p / SPAWN_FRAC, 0, 1) * state.meta.archive_day_count;
    const yb = state.meta.year_boundaries;
    let y = Object.keys(yb)[0];
    for (const k of Object.keys(yb)) if (day >= yb[k]) y = k;
    return y;
  }

  function render(ms) {
    if (!state.dots) return;
    const inBuild = ms <= DURATION_MS;
    let effMs, globalFade;
    if (inBuild) {
      effMs = ms; globalFade = 1;
    } else if (ms <= DURATION_MS + HOLD_MS) {
      effMs = DURATION_MS; globalFade = 1;
    } else {
      effMs = DURATION_MS;
      globalFade = clamp(1 - (ms - DURATION_MS - HOLD_MS) / FADE_MS, 0, 1);
    }
    background();
    ctx.globalAlpha = globalFade;
    drawChrome();
    ctx.globalAlpha = 1;
    const shown = drawDots(effMs, globalFade);
    const settled = effMs >= DURATION_MS;
    mCount.textContent = (settled ? state.dots.n : shown).toLocaleString("en-US");
    yrLabel.textContent = settled ? "2026" : yearFor(effMs / DURATION_MS);
    scrub.value = String(Math.round((ms / state.totalMs) * 1000));
  }

  function frame(now) {
    if (!state.playing) return;
    let ms = now - state.started;
    if (ms >= state.totalMs) {
      state.started = now;
      ms = 0;
    }
    state.playhead = ms;
    render(ms);
    state.raf = requestAnimationFrame(frame);
  }

  function play() {
    if (state.raf) cancelAnimationFrame(state.raf);
    state.playing = true;
    state.started = performance.now() - state.playhead;
    btnPlay.textContent = "Pause";
    btnPlay.setAttribute("aria-label", "Pause");
    state.raf = requestAnimationFrame(frame);
  }
  function pause() {
    if (state.raf) cancelAnimationFrame(state.raf);
    state.raf = 0;
    state.playing = false;
    btnPlay.textContent = "Play";
    btnPlay.setAttribute("aria-label", "Play");
  }
  function toggle() { state.playing ? pause() : play(); }
  function replay() { pause(); state.playhead = 0; render(0); play(); }

  function bind() {
    btnPlay.addEventListener("click", toggle);
    btnReplay.addEventListener("click", replay);
    canvas.addEventListener("click", toggle);
    window.addEventListener("keydown", (e) => {
      if (e.code === "Space" && e.target !== scrub) { e.preventDefault(); toggle(); }
    });
    scrub.addEventListener("input", () => {
      pause();
      state.playhead = (Number(scrub.value) / 1000) * state.totalMs;
      render(state.playhead);
    });
    window.addEventListener("resize", resize);
    reduceMotion.addEventListener("change", (e) => {
      if (e.matches) { pause(); state.playhead = DURATION_MS; render(state.playhead); }
    });
  }

  async function init() {
    try {
      const res = await fetch(DATA_URL, { cache: "no-store" });
      if (!res.ok) throw new Error("data " + res.status);
      const payload = await res.json();
      prepareDots(payload);
      bind();
      resize();
      if (reduceMotion.matches) {
        state.playhead = DURATION_MS;
        render(state.playhead);
        btnPlay.textContent = "Play";
        btnPlay.setAttribute("aria-label", "Play");
      } else {
        play();
      }
    } catch (err) {
      background();
      ctx.fillStyle = "#f4efe6";
      ctx.font = "16px Inter, sans-serif";
      ctx.fillText("Unable to load reply clock data.", 28, 44);
      ctx.fillStyle = "#b2b0be";
      ctx.fillText(String(err.message || err), 28, 70);
    }
  }

  init();
})();
