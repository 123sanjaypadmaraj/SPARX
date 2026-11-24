// Sparx web UI. Plain JavaScript, no build step, nothing loaded from outside this server.
// Text from the model, tools or other modules is only ever inserted as text nodes, never as HTML.
'use strict';
(() => {
  const $ = (sel, root = document) => root.querySelector(sel);
  const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

  /** Build an element. Props: class, text, on<event>, DOM properties, anything else as an attribute. */
  function h(tag, props, ...kids) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(props || {})) {
      if (v == null || v === false) continue;
      if (k === 'class') el.className = v;
      else if (k === 'text') el.textContent = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (k in el && k !== 'list') el[k] = v;      // input.list is read-only: it needs the attribute
      else el.setAttribute(k, v === true ? '' : v);
    }
    el.append(...kids.flat(3).filter((k) => k != null && k !== false));
    return el;
  }

  const store = {
    get(k, d) { try { return localStorage.getItem('sparx.' + k) ?? d; } catch { return d; } },
    set(k, v) { try { localStorage.setItem('sparx.' + k, v); } catch { /* private mode */ } },
  };

  const S = {
    state: null,            // last /api/state
    ws: null, connected: false, retry: 0, wantSocket: false,
    busy: false, stopped: false, listening: false, speaking: false,
    steps: null,            // tool chips of the turn in progress
    approvals: new Map(),   // id -> {card, deadline}
    view: 'chat',
    handsfree: false, speak: store.get('speak', '0') === '1', rec: null,
  };

  // ---------------------------------------------------------------- markdown (escape-by-construction)

  function link(href, label) {
    let ok = false;
    try { ok = ['http:', 'https:', 'mailto:'].includes(new URL(href).protocol); } catch { /* not a URL */ }
    return ok ? h('a', { href, target: '_blank', rel: 'noopener noreferrer', text: label }) : document.createTextNode(label);
  }

  const INLINE = /(`[^`\n]+`)|(\*\*[^*\n]+?\*\*)|(\*[^*\s][^*\n]*?\*)|(\[[^\]\n]+\]\((?:https?:\/\/|mailto:)[^\s)]+\))|(https?:\/\/[^\s<>"']+)/g;

  function inline(text, parent) {
    let last = 0;
    for (const m of text.matchAll(INLINE)) {
      let t = m[0];
      if (m[5]) t = t.replace(/[.,;:!?)\]]+$/, '');       // punctuation after a bare link is not part of it
      if (m.index > last) parent.append(text.slice(last, m.index));
      if (m[1]) parent.append(h('code', { text: t.slice(1, -1) }));
      else if (m[2]) inline(t.slice(2, -2), parent.appendChild(h('strong')));
      else if (m[3]) inline(t.slice(1, -1), parent.appendChild(h('em')));
      else if (m[4]) { const k = t.indexOf(']('); parent.append(link(t.slice(k + 2, -1), t.slice(1, k))); }
      else parent.append(link(t, t));
      last = m.index + t.length;
    }
    if (last < text.length) parent.append(text.slice(last));
    return parent;
  }

  function markdown(src) {
    const root = h('div', { class: 'md' });
    const lines = String(src ?? '').replace(/\r\n?/g, '\n').split('\n');
    let para = [], list = null, quote = [];
    const flush = () => {
      if (para.length) {
        const p = root.appendChild(h('p'));
        para.forEach((l, i) => { if (i) p.append(h('br')); inline(l, p); });
        para = [];
      }
      if (quote.length) { inline(quote.join(' '), root.appendChild(h('blockquote'))); quote = []; }
      list = null;
    };
    for (let i = 0; i < lines.length; i++) {
      const line = lines[i];
      let m;
      if (/^\s*```/.test(line)) {
        flush();
        const code = [];
        for (i++; i < lines.length && !/^\s*```\s*$/.test(lines[i]); i++) code.push(lines[i]);
        root.append(h('pre', null, h('code', { text: code.join('\n') })));
      } else if ((m = /^\s*(?:([-*+])|(\d+)[.)])\s+(.*)$/.exec(line))) {
        const tag = m[1] ? 'ul' : 'ol';
        if (!list || list.tagName.toLowerCase() !== tag) { flush(); list = root.appendChild(h(tag)); }
        inline(m[3], list.appendChild(h('li')));
      } else if ((m = /^#{1,6}\s+(.*)$/.exec(line))) {
        flush();
        inline(m[1], root.appendChild(h('h4')));
      } else if ((m = /^>\s?(.*)$/.exec(line))) {
        if (para.length || list) flush();
        quote.push(m[1]);
      } else if (!line.trim()) {
        flush();
      } else {
        if (list || quote.length) flush();
        para.push(line);
      }
    }
    flush();
    return root;
  }

  // ---------------------------------------------------------------- server calls

  async function api(path, { method = 'GET', body, form } = {}) {
    const opt = { method, credentials: 'same-origin', headers: {} };
    if (form) opt.body = form;
    else if (body !== undefined) { opt.headers['Content-Type'] = 'application/json'; opt.body = JSON.stringify(body); }
    let resp;
    try { resp = await fetch(path, opt); } catch { throw Object.assign(new Error('Sparx is not reachable.'), { status: 0 }); }
    let data = null;
    try { data = await resp.json(); } catch { /* no body */ }
    const detail = (data && typeof data.detail === 'string' && data.detail) || `Request failed (${resp.status}).`;
    if (resp.status === 401 && path !== '/api/login' && path !== '/api/unlock') gate('login');
    if (resp.status === 423) gate('pin');
    if (!resp.ok) throw Object.assign(new Error(detail), { status: resp.status });
    return data;
  }

  function toast(title, body, bad) {
    const t = h('div', { class: 'toast' + (bad ? ' bad' : ''), onclick: () => t.remove() },
      h('b', { text: title }), body && h('small', { text: body }));
    $('#toasts').append(t);
    setTimeout(() => t.remove(), bad ? 9000 : 6000);
  }
  const oops = (e) => toast('Could not do that', e.message, true);

  /** Save an authenticated API response as a file. A plain link could not carry the session, so fetch it and hand over a Blob. */
  async function download(path, fallback) {
    let resp;
    try { resp = await fetch(path, { credentials: 'same-origin' }); } catch { throw Object.assign(new Error('Sparx is not reachable.'), { status: 0 }); }
    if (resp.status === 401) gate('login');
    if (resp.status === 423) gate('pin');
    if (!resp.ok) {
      let detail = `Request failed (${resp.status}).`;
      try { const d = await resp.json(); if (d && typeof d.detail === 'string') detail = d.detail; } catch { /* no body */ }
      throw Object.assign(new Error(detail), { status: resp.status });
    }
    const named = /filename="([^"]+)"/.exec(resp.headers.get('Content-Disposition') || '');
    const url = URL.createObjectURL(await resp.blob());
    const a = h('a', { href: url, download: named ? named[1] : fallback, hidden: true });
    document.body.append(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 10000);
  }

  // ---------------------------------------------------------------- sign-in gate

  function gate(mode, msg) {
    S.wantSocket = false;
    if (S.ws) S.ws.close();
    stopSpeaking();
    $('#app').hidden = true;
    $('#gate').hidden = false;
    $('#login-form').hidden = mode !== 'login';
    $('#pin-form').hidden = mode !== 'pin';
    if (msg !== undefined) $('#gate-msg').textContent = msg;
    const field = mode === 'pin' ? $('#pin-input') : $('#login-token');
    field.value = '';
    field.focus();
  }

  $('#login-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/login', { method: 'POST', body: { token: $('#login-token').value.trim() } });
      $('#gate-msg').textContent = '';
      boot();
    } catch (err) { $('#gate-msg').textContent = err.message; }
  });
  $('#pin-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    try {
      await api('/api/unlock', { method: 'POST', body: { pin: $('#pin-input').value } });
      $('#gate-msg').textContent = '';
      boot();
    } catch (err) { $('#gate-msg').textContent = err.message; $('#pin-input').value = ''; }
  });
  const logout = async () => { try { await api('/api/logout', { method: 'POST' }); } catch { /* already out */ } gate('login', ''); };
  $('#pin-logout').addEventListener('click', logout);

  // ---------------------------------------------------------------- the core

  const LABELS = { offline: 'Offline', stopped: 'Stopped', listening: 'Listening', speaking: 'Speaking', thinking: 'Working', idle: 'Online' };

  function setCore() {
    const st = !S.connected ? 'offline' : S.stopped ? 'stopped' : S.listening ? 'listening'
      : S.speaking ? 'speaking' : S.busy ? 'thinking' : 'idle';
    document.body.dataset.state = st;
    $('#state-label').textContent = LABELS[st].toUpperCase();
    $('#link').textContent = S.connected ? (S.stopped ? 'stopped' : 'online') : 'offline';
    $('#send').disabled = S.busy || !S.connected;
    $('#cancel').hidden = !S.busy;
    $('#stop').classList.toggle('resume', S.stopped);
    $('#stop-label').textContent = S.stopped ? 'Resume' : 'Stop';
    $('#stop').title = S.stopped ? 'Clear the emergency stop so Sparx can work again'
      : 'Emergency stop: halts everything Sparx is doing';
  }
  const activity = (text) => { $('#activity').textContent = text || ''; };
  const caption = (text) => { $('#caption').textContent = String(text ?? '').replace(/```[\s\S]*?```/g, ' (code is in the conversation) ').replace(/[*_`#>]/g, '').trim().slice(0, 600); };

  function setOrbMode(full) {
    document.body.dataset.orb = full ? 'full' : 'normal';
    const b = $('#orb-toggle');
    b.setAttribute('aria-pressed', String(full));
    b.textContent = full ? 'Exit orb mode' : 'Orb mode';
    store.set('orb', full ? 'full' : 'normal');
  }
  $('#orb-toggle').addEventListener('click', () => setOrbMode(document.body.dataset.orb !== 'full'));
  setOrbMode(store.get('orb', 'normal') === 'full');

  // ---------------------------------------------------------------- conversation

  const log = $('#log');
  function scrollDown() { log.scrollTop = log.scrollHeight; }
  function clearEmpty() { const e = $('.empty', log); if (e) e.remove(); }
  function addMsg(kind, text) {
    clearEmpty();
    const el = h('div', { class: 'msg ' + kind });
    if (kind === 'assistant') el.append(markdown(text)); else el.textContent = text;
    log.append(el);
    scrollDown();
    return el;
  }

  async function loadMessages() {
    const cid = S.state && S.state.conversation;
    const turn = S.loading = (S.loading || 0) + 1;
    log.replaceChildren();
    S.steps = null;
    let msgs = [];
    if (cid) { try { msgs = await api(`/api/conversations/${encodeURIComponent(cid)}/messages`); } catch { return; } }
    if (turn !== S.loading) return;       // a newer load started while this one waited; it draws, this one must not
    if (!msgs.length) {
      log.append(h('div', { class: 'empty', text: 'A new conversation. Ask for anything: a plan for today, a file summarised, a reminder set.' }));
      return;
    }
    for (const m of msgs) addMsg(m.role === 'user' ? 'user' : 'assistant', m.content);
  }

  function endTurn(note) {
    S.busy = false;
    S.steps = null;
    for (const [id] of S.approvals) resolveApproval(id, null);
    activity('');
    if (note) addMsg('note', note);
    setCore();
  }

  function approvalCard(ev) {
    clearEmpty();
    const answer = (allow) => { sendWs({ type: 'approval', id: ev.id, allow }); $$('button', card).forEach((b) => { b.disabled = true; }); };
    const left = h('span', { class: 'left' });
    const card = h('div', { class: 'approval' },
      h('div', { class: 'approval-head' }, h('span', { text: 'Sparx wants to run' }), h('code', { text: ev.tool }),
        h('span', { class: 'risk ' + ev.risk, text: ev.risk })),
      ev.description && h('p', { text: ev.description }),
      h('pre', { text: JSON.stringify(ev.args ?? {}, null, 2) }),
      h('div', { class: 'approval-actions' },
        h('button', { class: 'btn primary', type: 'button', text: 'Allow', onclick: () => answer(true) }),
        h('button', { class: 'btn danger', type: 'button', text: 'Deny', onclick: () => answer(false) }), left));
    S.approvals.set(ev.id, { card, left, deadline: Date.now() + (Number(ev.timeout) || 120) * 1000 });
    log.append(card);
    scrollDown();
    activity(`Waiting for your approval: ${ev.tool}`);
    if (document.hidden) toast('Approval needed', `${ev.tool} (${ev.risk})`);
  }

  function resolveApproval(id, ev) {
    const a = S.approvals.get(id);
    if (!a) return;
    S.approvals.delete(id);
    a.card.classList.add('done');
    $$('button', a.card).forEach((b) => b.remove());
    a.left.textContent = !ev ? 'Cancelled' : ev.allow ? 'Allowed' : ev.reason === 'timed out' ? 'No answer in time: not run' : 'Denied';
  }

  setInterval(() => {
    for (const a of S.approvals.values()) {
      const s = Math.max(0, Math.round((a.deadline - Date.now()) / 1000));
      a.left.textContent = `Denied automatically in ${s}s`;
    }
  }, 1000);

  function onEvent(ev) {
    switch (ev.type) {
      case 'hello':
        S.stopped = !!ev.stop;
        if (S.state && ev.conversation && ev.conversation !== S.state.conversation) { S.state.conversation = ev.conversation; loadMessages(); }
        break;
      case 'turn_start':
        S.busy = true; S.steps = null;
        addMsg('user', ev.text);
        activity('Thinking…');
        break;
      case 'tool_start': {
        clearEmpty();
        if (!S.steps) { S.steps = h('div', { class: 'steps' }); log.append(S.steps); }
        S.steps.append(h('span', { class: 'step run', text: ev.tool, title: JSON.stringify(ev.args ?? {}) }));
        activity(`Running ${ev.tool}…`);
        scrollDown();
        break;
      }
      case 'tool_end': {
        const chip = S.steps && $$('.step.run', S.steps).find((c) => c.textContent === ev.tool);
        if (chip) { chip.className = 'step' + (ev.ok ? '' : ' fail'); chip.textContent = `${ev.tool} ${ev.ok ? '' : 'failed '}${ev.ms}ms`; }
        activity('Thinking…');
        break;
      }
      case 'approval_request': approvalCard(ev); break;
      case 'approval_resolved': resolveApproval(ev.id, ev); activity('Thinking…'); break;
      case 'assistant':
        endTurn();
        addMsg('assistant', ev.text);
        caption(ev.text);
        if (S.state) { S.state.conversation = ev.conversation; S.state.loaded = ev.loaded || S.state.loaded; renderSparx(); }
        say(ev.text);
        break;
      case 'error':
        if (ev.busy) { toast('Sparx is still working', ev.text); break; }
        endTurn();
        addMsg('error', ev.text);
        break;
      case 'cancelled': endTurn('Cancelled.'); break;
      case 'stopped': S.stopped = true; stopSpeaking(); break;
      case 'resumed': S.stopped = false; break;
      case 'notification': {
        const n = ev.notification || {};
        toast(n.title || 'Notification', [n.body, n.why && `Why: ${n.why}`].filter(Boolean).join(' · '));
        bumpUnread(1);
        refreshState().then(() => { if (S.view === 'pending' || S.view === 'automations') show(S.view); });     // a queued action or a paused run
        if (S.view === 'alerts') show('alerts');
        break;
      }
      case 'refresh':        // another device changed the conversation
        refreshState().then(() => { if (!S.busy) loadMessages(); });
        break;
      default: break;
    }
    setCore();
  }

  function sendWs(msg) {
    if (!S.ws || S.ws.readyState !== WebSocket.OPEN) { toast('Not connected', 'Sparx is not reachable right now.', true); return false; }
    S.ws.send(JSON.stringify(msg));
    return true;
  }

  function connect() {
    S.wantSocket = true;
    if (S.ws && S.ws.readyState <= WebSocket.OPEN) return;
    const ws = new WebSocket(`${location.protocol === 'https:' ? 'wss' : 'ws'}://${location.host}/ws`);
    S.ws = ws;
    ws.onopen = () => { S.connected = true; S.retry = 0; setCore(); };
    ws.onmessage = (m) => { let ev; try { ev = JSON.parse(m.data); } catch { return; } if (ev && typeof ev === 'object') onEvent(ev); };
    ws.onclose = (e) => {
      if (S.ws !== ws) return;
      S.connected = false; S.ws = null;
      if (S.busy) endTurn('Connection lost. The turn was stopped.'); else setCore();
      if (S.wantSocket) reconnect();
    };
  }

  /** Try again with a growing delay. Asking /api/state first tells "server down" from "signed out" or "locked". */
  let retryTimer = 0;
  function reconnect() {
    clearTimeout(retryTimer);
    S.retry++;
    retryTimer = setTimeout(async () => {
      if (!S.wantSocket || S.connected) return;
      try { S.state = await api('/api/state'); } catch (e) {
        if (!e.status || e.status >= 500) reconnect();       // 401 / 423 have already put up the sign-in or PIN screen
        return;
      }
      renderSparx();
      connect();
      loadMessages();
    }, Math.min(15000, 500 * 2 ** Math.min(S.retry, 5)));
  }
  setInterval(() => { if (S.connected) S.ws.send('{"type":"ping"}'); }, 25000);

  // composer
  const input = $('#input');
  function fitInput() { input.style.height = 'auto'; input.style.height = Math.min(input.scrollHeight + 2, 160) + 'px'; }
  function submit(text) {
    text = (text ?? input.value).trim();
    if (!text || S.busy) return false;
    stopSpeaking();
    if (!sendWs({ type: 'user', text })) return false;
    input.value = '';
    fitInput();
    return true;
  }
  $('#composer').addEventListener('submit', (e) => { e.preventDefault(); submit(); });
  input.addEventListener('input', fitInput);
  input.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && !e.shiftKey && !e.isComposing) { e.preventDefault(); submit(); }
  });
  $('#cancel').addEventListener('click', () => sendWs({ type: 'cancel' }));
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape') stopSpeaking(); });
  $('#core').addEventListener('click', stopSpeaking);

  $('#stop').addEventListener('click', async () => {
    stopSpeaking();
    try {
      const r = await api(S.stopped ? '/api/resume' : '/api/stop', { method: 'POST' });
      S.stopped = !!r.stop;
      setCore();
      loadGreeting();
    } catch (e) { oops(e); }
  });

  $('#file').addEventListener('change', async (e) => {
    const file = e.target.files[0];
    e.target.value = '';
    if (!file) return;
    const form = new FormData();
    form.append('file', file, file.name);
    activity(`Uploading ${file.name}…`);
    try {
      const r = await api('/api/upload', { method: 'POST', form });
      input.value = `${input.value}${input.value ? ' ' : ''}I uploaded the file "${r.name}" (saved at ${r.path}). `;
      fitInput();
      input.focus();
      toast('Uploaded', `${r.name} · ${Math.max(1, Math.round(r.size / 1024))} KB`);
    } catch (err) { oops(err); }
    activity('');
  });

  // ---------------------------------------------------------------- voice (Web Speech API, where the browser has it)

  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  const TTS = 'speechSynthesis' in window && 'SpeechSynthesisUtterance' in window;
  const voiceLang = $('#voice-lang');

  function spoke() {          // Sparx stopped talking, by finishing or by being interrupted
    if (!S.speaking) return;
    S.speaking = false;
    setCore();
    if (S.handsfree) setTimeout(() => S.handsfree && !S.speaking && listen('free'), 250);
  }
  function stopSpeaking() {
    if (TTS) speechSynthesis.cancel();
    spoke();
  }

  function say(text) {
    if (!TTS || !(S.speak || S.handsfree)) return;
    const plain = String(text).replace(/```[\s\S]*?```/g, ' (code is on screen) ').replace(/\[([^\]]+)\]\([^)]+\)/g, '$1')
      .replace(/https?:\/\/\S+/g, 'link').replace(/[*_`#>]/g, '').trim().slice(0, 1500);
    if (!plain) return;
    speechSynthesis.cancel();
    const u = new SpeechSynthesisUtterance(plain);
    u.lang = voiceLang.value;
    const voice = speechSynthesis.getVoices().find((v) => v.lang === u.lang) || speechSynthesis.getVoices().find((v) => v.lang.startsWith(u.lang.slice(0, 2)));
    if (voice) u.voice = voice;
    u.onstart = () => { S.speaking = true; if (S.rec) S.rec.abort(); setCore(); };     // do not let Sparx hear itself
    u.onend = spoke;
    u.onerror = spoke;
    speechSynthesis.speak(u);
  }

  function listen(mode) {
    if (!SR || S.rec) return;
    if (TTS) speechSynthesis.cancel();      // barge-in: the user talking always silences Sparx
    S.speaking = false;
    const rec = new SR();
    let finalText = '';
    rec.lang = voiceLang.value;
    rec.interimResults = true;
    rec.continuous = mode === 'free';
    rec.onstart = () => { S.listening = true; $('#mic').classList.add('on'); setCore(); };
    rec.onspeechstart = () => { if (TTS) speechSynthesis.cancel(); };
    rec.onresult = (e) => {
      let interim = '';
      for (let i = e.resultIndex; i < e.results.length; i++) {
        if (e.results[i].isFinal) finalText += e.results[i][0].transcript; else interim += e.results[i][0].transcript;
      }
      input.value = (finalText + interim).trim();
      fitInput();
      if (window.SparxOrb) SparxOrb.pulse(0.5 + Math.random() * 0.4);
      if (mode === 'free' && finalText.trim() && !interim && !S.busy) { finalText = ''; submit(); }
    };
    rec.onerror = (e) => {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed') {
        setHandsfree(false);
        toast('Microphone unavailable', window.isSecureContext ? 'Allow microphone access for this page in the browser.'
          : 'Browsers only allow the microphone over HTTPS or on localhost. Reach Sparx through an HTTPS tunnel or Tailscale.', true);
      } else if (e.error === 'language-not-supported') {
        toast('Language not supported', 'This browser cannot recognise speech in that language.', true);
      } else if (e.error === 'network') {
        setHandsfree(false);
        toast('Speech recognition unavailable', 'This browser sends speech to an online service that is not reachable.', true);
      }
    };
    rec.onend = () => {
      S.rec = null; S.listening = false; $('#mic').classList.remove('on'); setCore();
      if (mode === 'ptt') submit();
      else if (S.handsfree && !S.speaking) setTimeout(() => S.handsfree && !S.speaking && listen('free'), 400);
    };
    S.rec = rec;
    try { rec.start(); } catch { S.rec = null; }
  }

  function setHandsfree(on) {
    S.handsfree = on;
    const b = $('#handsfree');
    b.setAttribute('aria-pressed', String(on));
    b.textContent = `Hands-free: ${on ? 'on' : 'off'}`;
    if (on) listen('free'); else if (S.rec) S.rec.stop();
  }

  function setupVoice() {
    voiceLang.value = store.get('lang', { hi: 'hi-IN', kn: 'kn-IN' }[S.state.language] || 'en-IN');
    voiceLang.addEventListener('change', () => { store.set('lang', voiceLang.value); if (S.rec) S.rec.stop(); });
    $('#voice-row').hidden = !(SR || TTS);
    voiceLang.hidden = !(SR || TTS);
    if (SR) {
      const mic = $('#mic');
      mic.hidden = false;
      mic.addEventListener('pointerdown', (e) => { e.preventDefault(); if (S.handsfree) setHandsfree(false); listen('ptt'); });
      for (const up of ['pointerup', 'pointercancel', 'pointerleave']) mic.addEventListener(up, () => { if (S.rec && !S.handsfree) S.rec.stop(); });
      mic.addEventListener('keydown', (e) => {             // keyboard: Space/Enter toggles
        if (e.key !== ' ' && e.key !== 'Enter' || e.repeat) return;
        e.preventDefault();
        if (S.rec) S.rec.stop(); else listen('ptt');
      });
      mic.addEventListener('contextmenu', (e) => e.preventDefault());
      $('#handsfree').addEventListener('click', () => setHandsfree(!S.handsfree));
    } else {
      $('#handsfree').hidden = true;
    }
    if (TTS) {
      const b = $('#speak-toggle');
      const paint = () => { b.setAttribute('aria-pressed', String(S.speak)); b.textContent = `Speak replies: ${S.speak ? 'on' : 'off'}`; };
      b.hidden = false;
      paint();
      b.addEventListener('click', () => { S.speak = !S.speak; store.set('speak', S.speak ? '1' : '0'); if (!S.speak) stopSpeaking(); paint(); });
      speechSynthesis.getVoices();        // some browsers load the voice list lazily
    }
  }

  // ---------------------------------------------------------------- HUD data: greeting, protocols, systems

  async function loadGreeting() {
    try { $('#greeting').textContent = (await api('/api/greeting')).greeting; } catch { /* keep the last one */ }
  }

  async function loadProtocols() {
    const box = $('#protocols');
    let list = [];
    try { list = (await api('/api/protocols')).protocols || []; } catch { /* strip stays hidden */ }
    box.hidden = !list.length;
    box.replaceChildren(h('span', { class: 'label', text: 'Protocols' }), ...list.map((p) => h('button', {
      type: 'button', text: p.name, title: p.description || `Run the ${p.name} protocol`,
      onclick: () => { if (!submit(`Run the ${p.name} protocol.`) && S.busy) toast('Sparx is still working', 'Wait for the current turn or cancel it.'); },
    })));
  }

  const gauges = new Map();
  function gauge(box, key, label, pct, detail) {
    let g = gauges.get(key);
    if (!g) {
      g = { val: h('b'), fill: h('i'), bar: h('div', { class: 'bar' }) };
      g.bar.append(g.fill);
      g.root = h('div', { class: 'gauge' }, h('div', { class: 'gauge-head' }, h('span', { text: label }), g.val), g.bar);
      gauges.set(key, g);
    }
    if (g.root.parentNode !== box) box.append(g.root);
    pct = Math.max(0, Math.min(100, Number(pct) || 0));
    g.val.textContent = detail;
    g.fill.style.width = pct + '%';
    g.bar.className = 'bar' + (pct >= 92 ? ' crit' : pct >= 80 ? ' warn' : '');
    g.seen = true;
  }

  function renderVitals(v) {
    const box = $('#vitals');
    if (!v || !v.available) {
      gauges.clear();
      box.replaceChildren(h('div', { class: 'empty', text: v && v.error ? `Machine vitals could not be read: ${v.error}`
        : 'Machine vitals are not available on this install.' }));
      return;
    }
    if ($('.empty', box)) box.replaceChildren();
    for (const g of gauges.values()) g.seen = false;
    const num = (x, d = 0) => (typeof x === 'number' ? x.toFixed(d) : '?');
    if ('cpu_percent' in v) gauge(box, 'cpu', 'CPU', v.cpu_percent, `${num(v.cpu_percent)}%${v.cpu_cores ? ` · ${v.cpu_cores} cores` : ''}`);
    if ('memory_percent' in v) gauge(box, 'mem', 'Memory', v.memory_percent, `${num(v.memory_used_gb, 1)} / ${num(v.memory_total_gb, 1)} GB`);
    for (const d of Array.isArray(v.disks) ? v.disks : []) gauge(box, 'disk' + d.mount, `Disk ${d.mount}`, d.percent, `${num(d.free_gb, 1)} GB free`);
    if (v.battery) gauge(box, 'bat', 'Battery', v.battery.percent, `${num(v.battery.percent)}%${v.battery.plugged ? ' · charging' : ''}`);
    for (const [k, g] of gauges) if (!g.seen) { g.root.remove(); gauges.delete(k); }
    let extra = $('.vitals-extra', box);
    if (!extra) extra = box.appendChild(h('div', { class: 'vitals-extra' }));
    box.append(extra);
    const kv = h('dl', { class: 'kv' });
    const row = (k, val) => kv.append(h('dt', { text: k }), h('dd', { text: val }));
    if (!v.battery && 'battery' in v) row('Power', 'mains');
    if (v.net) row('Network', `↑ ${num(v.net.sent_mb, 0)} MB  ↓ ${num(v.net.recv_mb, 0)} MB`);
    if ('uptime_hours' in v) row('Uptime', `${num(v.uptime_hours, 1)} h`);
    const procs = h('div', { class: 'procs' });
    for (const p of (Array.isArray(v.top_processes) ? v.top_processes : []).slice(0, 5)) {
      procs.append(h('span', { text: String(p.name) }), h('span', { text: `${num(p.cpu)}%` }), h('span', { text: `${num(p.memory_mb)} MB` }));
    }
    extra.replaceChildren(kv, procs.children.length ? h('h3', { text: 'Busiest processes' }) : null, procs);
  }

  function systemsVisible() { return !document.hidden && !$('#app').hidden && getComputedStyle($('.systems')).display !== 'none'; }
  let vitalsBusy = false;
  async function pollVitals() {
    if (vitalsBusy || !systemsVisible()) return;
    vitalsBusy = true;
    try { renderVitals(await api('/api/vitals')); } catch { /* next round */ }
    vitalsBusy = false;
  }
  setInterval(pollVitals, 6000);

  function bumpUnread(by) {
    if (!S.state) return;
    S.state.unread = Math.max(0, (S.state.unread || 0) + by);
    const b = $('#unread');
    b.hidden = !S.state.unread;
    b.textContent = S.state.unread;
  }

  function renderSparx() {
    const s = S.state;
    if (!s) return;
    document.title = s.name;
    $('#brand-name').textContent = s.name;
    input.placeholder = `Ask ${s.name}…`;
    $('#model').textContent = s.model;
    bumpUnread(0);
    const waiting = $('#pending-count');
    waiting.hidden = !s.pending;
    waiting.textContent = s.pending || '';
    const runs = $('#automations-count');
    runs.hidden = !s.waiting_runs;
    runs.textContent = s.waiting_runs || '';
    const scope = $('#scope');
    const scopes = [...new Set(['personal', 'work', 'study', s.scope])];
    scope.replaceChildren(...scopes.map((x) => h('option', { value: x, text: x })), h('option', { value: '+', text: 'New scope…' }));
    scope.value = s.scope;
    const dl = $('#sys-sparx');
    dl.replaceChildren();
    const row = (k, v) => dl.append(h('dt', { text: k }), h('dd', { text: v, title: v }));
    row('Model', s.model);
    row('Version', s.version);
    row('Memory scope', s.scope);
    row('Background daemon', s.daemon ? (s.daemon.age_seconds != null ? `seen ${s.daemon.age_seconds}s ago` : 'heartbeat found') : 'no heartbeat');
    row('Emergency stop', s.stop ? 'SET' : 'clear');
    row('Tools', String(s.groups.reduce((n, g) => n + g.tools, 0)));
    const errs = Object.keys(s.load_errors || {});
    if (errs.length) row('Modules not loaded', errs.join(', '));
    const loaded = new Set(s.loaded);
    $('#groups').replaceChildren(...s.groups.map((g) => h('span', {
      class: loaded.has(g.name) ? 'on' : '', text: g.name, title: `${g.about || g.name} · ${g.tools} tools${loaded.has(g.name) ? ' · loaded' : ''}`,
    })));
  }

  async function setScope(scope) {
    if (scope) { try { S.state.scope = (await api('/api/scope', { method: 'POST', body: { scope } })).scope; } catch (err) { oops(err); } }
    renderSparx();
    if (S.view === 'memory') show('memory');
  }
  const scopeNew = $('#scope-new');
  const closeScopeNew = () => { scopeNew.hidden = true; scopeNew.value = ''; };
  $('#scope').addEventListener('change', (e) => {
    if (e.target.value !== '+') { closeScopeNew(); return setScope(e.target.value); }
    scopeNew.hidden = false;        // typed in the page: native dialogs are blocked in an installed app
    scopeNew.focus();
  });
  scopeNew.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); const name = scopeNew.value.trim(); closeScopeNew(); setScope(name); }
    else if (e.key === 'Escape') { closeScopeNew(); renderSparx(); }
  });
  scopeNew.addEventListener('blur', () => { if (!scopeNew.hidden && !scopeNew.value.trim()) { closeScopeNew(); renderSparx(); } });

  async function refreshState() {
    try { S.state = await api('/api/state'); } catch { return; }
    S.stopped = !!S.state.stop;
    renderSparx();
    setCore();
  }
  setInterval(() => { if (!document.hidden && !$('#app').hidden) refreshState(); }, 30000);

  // ---------------------------------------------------------------- panels

  const TITLE_KEYS = ['title', 'name', 'text', 'content', 'what', 'description', 'summary', 'body', 'label'];
  const fmt = (v) => (v !== null && typeof v === 'object' ? JSON.stringify(v) : String(v));
  const empty = (text) => h('div', { class: 'empty', text });

  /** A row from a table this UI knows nothing about: best-guess title, every other column as "key: value". */
  function rowCard(r, extra) {
    const tk = TITLE_KEYS.find((k) => r[k] != null && r[k] !== '');
    const meta = h('div', { class: 'card-meta' });
    for (const [k, v] of Object.entries(r)) {
      if (k === tk || v == null || v === '') continue;
      meta.append(`${k.replace(/_/g, ' ')}: `, h('b', { text: fmt(v).slice(0, 200) }), '   ');
    }
    return h('div', { class: 'card' }, tk && h('div', { class: 'card-title', text: fmt(r[tk]) }), meta, extra);
  }

  function fill(box, items, render, none) {
    box.replaceChildren(...(items.length ? items.map(render) : [empty(none)]));
  }

  function noteCard(n) {
    return h('div', { class: 'card' + (n.read ? '' : ' unread') },
      h('div', { class: 'card-title', text: n.title }), h('div', { text: n.body }),
      n.why && h('div', { class: 'card-why', text: `Why: ${n.why}` }),
      h('div', { class: 'card-meta', text: `${n.kind} · ${n.ts}` }));
  }

  const PANELS = {
    async today(box) {
      const t = await api('/api/today');
      const part = (title, items, none) => [h('div', { class: 'section-title', text: `${title} (${items.length})` }),
        ...(items.length ? items.map((r) => rowCard(r)) : [empty(none)])];
      box.replaceChildren(h('p', { class: 'hint', text: new Date(t.date + 'T12:00').toLocaleDateString(undefined, { weekday: 'long', day: 'numeric', month: 'long' }) }),
        ...part('Events', t.events, 'Nothing on the calendar today.'),
        ...part('Open tasks', t.tasks, 'No open tasks. Ask Sparx to add one.'),
        ...part('Reminders', t.reminders, 'No pending reminders.'),
        h('div', { class: 'section-title', text: `Unread notifications (${t.notifications.length})` }),
        ...(t.notifications.length ? t.notifications.map(noteCard) : [empty('Nothing new.')]));
    },

    async tasks(box) {
      const kind = $('#dash-kind').value;
      const d = await api(`/api/dash/${kind}?open_only=${$('#dash-open').checked}`);
      fill(box, d.rows, (r) => rowCard(r), `No ${kind} yet. Ask Sparx to add some: "remind me to…", "add a task…".`);
    },

    async memory(box) {
      const scope = $('#memory-all').checked ? '' : S.state.scope;
      const m = await api(`/api/memory?scope=${encodeURIComponent(scope)}`);
      fill(box, m.items, (item) => {
        const id = item.id ?? item.memory_id ?? item.key;
        const actions = h('div', { class: 'card-actions' });
        const card = rowCard(item, actions);
        const reload = () => show('memory');
        if (m.editable && id != null) {
          actions.append(h('button', { class: 'btn small', type: 'button', text: 'Correct', onclick: () => {
            const ta = h('textarea', { rows: 3, value: fmt(item.text ?? item.content ?? item.value ?? '') });
            actions.replaceChildren(
              h('button', { class: 'btn small primary', type: 'button', text: 'Save', onclick: async () => {
                try { await api(`/api/memory/${encodeURIComponent(id)}`, { method: 'PATCH', body: { text: ta.value } }); reload(); } catch (e) { oops(e); }
              } }),
              h('button', { class: 'btn small ghost', type: 'button', text: 'Cancel', onclick: reload }));
            card.insertBefore(ta, actions);
            ta.focus();
          } }));
        }
        if (m.deletable && id != null) {
          actions.append(h('button', { class: 'btn small danger', type: 'button', text: 'Forget', onclick: () => {
            const no = h('button', { class: 'btn small ghost', type: 'button', text: 'Cancel', onclick: reload });
            actions.replaceChildren(
              h('span', { class: 'card-confirm', role: 'alert', text: 'Forget this memory? You can restore it from Undo.' }),
              h('button', { class: 'btn small danger', type: 'button', text: 'Yes, forget', onclick: async (e) => {
                e.target.disabled = true;
                try { await api(`/api/memory/${encodeURIComponent(id)}`, { method: 'DELETE' }); } catch (err) { oops(err); }
                reload();
              } }), no);
            no.focus();
          } }));
        }
        return card;
      }, m.text || (scope ? `Nothing remembered in the "${scope}" scope yet.` : 'Nothing remembered yet.'));
    },

    async alerts(box) {
      fill(box, await api('/api/notifications?all=true'), noteCard,
        'No notifications. Reminders, briefings and system alerts appear here, each with the reason it was sent.');
    },

    async pending(box) {
      const d = await api('/api/pending');
      const act = (id, what, done) => async (e) => {
        e.target.disabled = true;
        try { const r = await api(`/api/pending/${id}/${what}`, { method: 'POST' }); toast(done, String(r.result).slice(0, 200)); }
        catch (err) { oops(err); }
        await refreshState();
        show('pending');
      };
      const card = (p) => h('div', { class: 'card pending-card' },
        h('div', { class: 'card-title' }, h('code', { text: p.tool }), ' ', h('span', { class: 'risk ' + p.risk, text: p.risk || '?' })),
        p.description && h('div', { text: p.description }),
        h('pre', { text: JSON.stringify(p.args, null, 1) }),
        h('div', { class: 'card-meta', text: `asked by ${p.agent || 'a background job'} at ${p.ts.slice(11, 16)}, expires ${p.expires.slice(11, 16)}` }),
        h('div', { class: 'card-actions' },
          h('button', { class: 'btn small', type: 'button', text: 'Run now', onclick: act(p.id, 'approve', 'Action run') }),
          h('button', { class: 'btn small ghost', type: 'button', text: 'Dismiss', onclick: act(p.id, 'dismiss', 'Dismissed') })));
      box.replaceChildren(
        h('div', { class: 'section-title', text: 'Waiting for you' }),
        ...(d.pending.length ? d.pending.map(card)
          : [empty('Nothing is waiting. Actions a background job was not allowed to run on its own appear here until they expire.')]),
        h('div', { class: 'section-title', text: 'Recently handled' }),
        ...(d.recent.length ? d.recent.map((p) => h('div', { class: 'card' },
          h('div', { class: 'card-title' }, h('code', { text: p.tool }), ' ', h('span', { class: 'chip dim', text: p.status })),
          p.result && h('div', { class: 'card-meta', text: p.result.slice(0, 300) }),
          h('div', { class: 'card-meta', text: `${JSON.stringify(p.args)} · ${p.ts}` }))) : [empty('Nothing yet.')]));
    },

    async automations(box) {
      const d = await api('/api/automations');
      const when = (ts) => String(ts || '').replace('T', ' ').slice(0, 16);
      const act = (path, body, done) => async (e) => {
        e.target.disabled = true;
        try { const r = await api(`/api/automations/${path}`, { method: 'POST', body }); toast(done, r.status ? `The run is now ${r.status}.` : ''); }
        catch (err) { oops(err); }
        await refreshState();
        show('automations');
      };
      const trigger = (t) => (t.type === 'schedule' ? t.spec : [t.type, t.name || t.kind || t.path].filter(Boolean).join(': '));
      const waiting = d.runs.filter((r) => r.status === 'waiting_approval');
      const recent = d.runs.filter((r) => r.status !== 'waiting_approval');
      const waitCard = (r) => h('div', { class: 'card pending-card run-waiting' },
        h('div', { class: 'card-title', text: r.automation }),
        h('pre', { text: r.pending || 'The run did not record what it is waiting for.' }),
        r.then.length ? [h('div', { class: 'card-why', text: 'Approving lets the rest of the run go ahead too, without asking again:' }),
          h('pre', { class: 'run-then', text: r.then.join('\n') })] : null,
        h('div', { class: 'card-meta', text: `run ${r.id} · ${r.trigger} · started ${when(r.started)}` }),
        h('div', { class: 'card-actions' },
          h('button', { class: 'btn small', type: 'button', text: 'Approve and continue', onclick: act(`runs/${r.id}/resume`, undefined, 'Run continued') }),
          h('button', { class: 'btn small ghost', type: 'button', text: 'Cancel run', onclick: act(`runs/${r.id}/cancel`, undefined, 'Run cancelled') })));
      const autoCard = (a) => h('div', { class: 'card auto-card' },
        h('div', { class: 'card-title' }, a.name, ' ', h('span', { class: 'chip dim auto-state' + (a.enabled ? ' on' : ''), text: a.enabled ? 'on' : 'off' })),
        h('div', { class: 'card-meta', text: [trigger(a.trigger), `${a.steps.length} step${a.steps.length === 1 ? '' : 's'}`,
          a.next_run && `next run ${when(a.next_run)}`].filter(Boolean).join(' · ') }),
        h('div', { class: 'card-actions' }, h('button', { class: 'btn small' + (a.enabled ? ' ghost' : ''), type: 'button',
          text: a.enabled ? 'Disable' : 'Enable',
          onclick: act(`${encodeURIComponent(a.id)}/enabled`, { enabled: !a.enabled }, a.enabled ? 'Automation turned off' : 'Automation turned on') })));
      const runCard = (r) => h('div', { class: 'card run-card' },
        h('div', { class: 'card-title' }, r.automation, ' ', h('span', { class: 'chip dim', text: r.status })),
        h('div', { class: 'card-meta', text: `run ${r.id} · ${r.trigger} · ${when(r.started)}${r.finished ? ` to ${when(r.finished).slice(11)}` : ''}` }));
      box.replaceChildren(
        h('div', { class: 'section-title', text: 'Waiting for you' }),
        ...(waiting.length ? waiting.map(waitCard)
          : [empty('Nothing is waiting. A run that reaches a step it may not do without you pauses and appears here.')]),
        h('div', { class: 'section-title', text: 'Automations' }),
        ...(d.automations.length ? d.automations.map(autoCard)
          : [empty('No automations yet. Ask Sparx to make one: "every weekday at 9, list my tasks".')]),
        h('div', { class: 'section-title', text: 'Recent runs' }),
        ...(recent.length ? recent.map(runCard) : [empty('No runs yet.')]));
    },

    async history(box) {
      const c = await api('/api/conversations');
      fill(box, c.conversations, (v) => {
        const url = `/api/conversations/${encodeURIComponent(v.id)}`;
        const actions = h('div', { class: 'card-actions' });
        const save = (format) => h('button', { class: 'btn small', type: 'button', text: `Export ${format === 'md' ? 'Markdown' : 'JSON'}`, onclick: async () => {
          try { await download(`${url}/export?format=${format}`, `sparx-conversation.${format}`); } catch (e) { oops(e); }
        } });
        const idle = () => actions.replaceChildren(save('md'), save('json'),
          h('button', { class: 'btn small danger', type: 'button', text: 'Delete', onclick: ask }));
        function ask() {
          const no = h('button', { class: 'btn small ghost', type: 'button', text: 'Cancel', onclick: idle });
          actions.replaceChildren(
            h('span', { class: 'card-confirm', role: 'alert', text: `Delete this conversation (${v.messages} messages)? You can restore it from Undo.` }),
            h('button', { class: 'btn small danger', type: 'button', text: 'Yes, delete', onclick: async (e) => {
              e.target.disabled = true;
              let gone;
              try { gone = await api(url, { method: 'DELETE' }); } catch (err) { oops(err); return idle(); }
              toast('Conversation deleted', 'Restore it from Undo if that was a mistake.');
              if (gone && gone.current) S.state.conversation = gone.current;
              if (v.id === c.current && !S.busy) loadMessages();
              show('history');
            } }), no);
          no.focus();
        }
        idle();
        return h('div', { class: 'card' + (v.id === c.current ? ' current' : '') },
          h('button', {
            class: 'card-open', type: 'button', title: 'Open this conversation',
            onclick: async () => {
              try { await api(`${url}/select`, { method: 'POST' }); } catch (e) { return oops(e); }
              S.state.conversation = v.id;
              await loadMessages();
              show('chat');
            },
          }, h('div', { class: 'card-title', text: v.title || '(no text)' }),
          h('div', { class: 'card-meta', text: `${v.messages} messages · last ${v.updated}${v.id === c.current ? ' · current' : ''}` })),
          actions);
      }, 'No conversations yet.');
    },

    async activity(box) {
      fill(box, await api('/api/audit?limit=150'), (a) => h('div', { class: 'card' },
        h('div', { class: 'card-title' }, h('code', { text: a.tool }), ' ', h('span', { class: 'risk ' + a.risk, text: a.risk || '?' }), ' ',
          h('span', { class: a.ok ? 'ok' : 'bad', text: a.ok ? 'ok' : 'not done' })),
        a.summary && h('div', { text: a.summary }),
        h('div', { class: 'card-meta', text: `${a.ts} · ${a.agent || ''} · ${a.ms} ms · ${a.args || ''}` })),
      'Nothing yet. Every tool Sparx runs is recorded here.');
    },

    async undo(box) {
      fill(box, await api('/api/undo'), (u) => h('div', { class: 'card' },
        h('div', { class: 'card-title', text: u.description }),
        h('div', { class: 'card-meta', text: `${u.kind} · ${u.ts}${u.undone ? ' · undone' : ''}` }),
        !u.undone && h('div', { class: 'card-actions' }, h('button', { class: 'btn small', type: 'button', text: 'Undo', onclick: async () => {
          try { toast('Undo', (await api('/api/undo', { method: 'POST', body: { id: u.id } })).result); } catch (e) { oops(e); }
          show('undo');
        } }))),
      'Nothing to undo. Reversible actions (moved files, forgotten memories…) are listed here.');
    },

    async usage(box) {
      const u = await api('/api/usage');
      const l = u.llm;
      const dl = (pairs) => h('dl', { class: 'kv' }, pairs.map(([k, v]) => [h('dt', { text: k }), h('dd', { text: String(v) })]));
      const past = u.history || { rows: [], totals: {} };
      const t = past.totals;
      box.replaceChildren(
        h('div', { class: 'section-title', text: 'Model, last 30 days (kept across restarts)' }),
        ...(past.rows.length ? [
          h('div', { class: 'card' }, dl([['Calls', t.calls], ['Prompt tokens', t.prompt_tokens], ['Completion tokens', t.completion_tokens],
            ['Average response', t.calls ? `${Math.round(t.ms / t.calls)} ms` : 'n/a']]),
          t.unreported_calls > 0 ? h('div', { class: 'card-meta', text: `${t.unreported_calls} calls came back without token counts, so their tokens are unknown` }) : null),
          ...past.rows.map((r) => h('div', { class: 'card' }, h('div', { class: 'card-title', text: `${r.day} · ${r.model}` }),
            h('div', { class: 'card-meta', text: `${r.calls} calls · ${r.prompt_tokens} + ${r.completion_tokens} tokens` }))),
        ] : [empty('No model calls recorded yet.')]),
        h('div', { class: 'section-title', text: 'Model, since this server started' }),
        h('div', { class: 'card' }, dl([['Calls', l.calls], ['Prompt tokens', l.prompt_tokens], ['Completion tokens', l.completion_tokens],
          ['Average response', l.calls ? `${Math.round(l.ms / l.calls)} ms` : 'n/a']])),
        ...Object.entries(l.by_model || {}).map(([name, m]) => h('div', { class: 'card' }, h('div', { class: 'card-title', text: name }),
          h('div', { class: 'card-meta', text: `${m.calls} calls · ${m.prompt_tokens} + ${m.completion_tokens} tokens` }))),
        h('div', { class: 'section-title', text: 'Tools today' }),
        h('div', { class: 'card' }, dl([['Calls', u.tools_today.calls], ['Errors', u.tools_today.errors], ['Time', `${u.tools_today.ms} ms`]])),
        ...(u.top_tools.length ? u.top_tools.map((t) => h('div', { class: 'card' }, h('code', { text: t.tool }),
          h('span', { class: 'card-meta', text: `  ${t.calls} calls · ${t.errors} errors` }))) : [empty('No tools were used today.')]));
    },

    async settings(box) {
      const d = await api('/api/settings');
      const s = d.settings;
      const save = async (key, value, pin) => {
        try { await api('/api/settings', { method: 'PATCH', body: { changes: { [key]: value }, pin } }); toast('Saved', key); refreshState(); return true; }
        catch (e) { oops(e); show('settings'); return false; }
      };
      const field = (label, control) => h('label', { class: 'field' }, h('span', { text: label }), control);
      const text = (key, value) => h('input', { type: 'text', value: value ?? '', onchange: (e) => save(key, e.target.value) });
      const pick = (key, value, options) => h('select', { onchange: (e) => save(key, e.target.value) },
        [...new Set([...options, value])].map((o) => h('option', { value: o, text: o, selected: o === value })));
      // Model roles are free text, with the models that exist right now offered as suggestions.
      const found = await api('/api/models').catch(() => ({ models: [], providers: {}, kaggle: {} }));
      const suggest = h('datalist', { id: 'model-specs' }, found.models.map((m) => h('option', { value: m.spec, label: m.source === 'kaggle' ? `${m.spec} (Kaggle notebook)` : m.spec })));
      const modelText = (key, value) => h('input', { type: 'text', value: value ?? '', list: 'model-specs', placeholder: 'provider/model',
        onchange: (e) => save(key, e.target.value.trim()) });
      const auto = (group) => Object.entries(s[group] || {}).map(([k, v]) => field(k.replace(/_/g, ' '),
        group === 'models' && typeof v === 'string' ? modelText(`${group}.${k}`, v) :
        typeof v === 'boolean' ? h('input', { type: 'checkbox', checked: v, onchange: (e) => save(`${group}.${k}`, e.target.checked) })
          : typeof v === 'number' ? h('input', { type: 'number', value: v, onchange: (e) => save(`${group}.${k}`, Number(e.target.value)) })
            : text(`${group}.${k}`, v)));

      const pin = h('input', { type: 'password', inputmode: 'numeric', autocomplete: 'off', placeholder: 'PIN' });
      const perms = Object.entries(s.permissions || {}).map(([risk, policy]) => field(risk,
        h('select', { disabled: !d.pin_set, onchange: async (e) => { await save(`permissions.${risk}`, e.target.value, pin.value); } },
          ['allow', 'ask', 'deny'].map((o) => h('option', { value: o, text: o, selected: o === policy })))));

      const linkBox = h('div', { class: 'card', hidden: true });
      const local = ['localhost', '127.0.0.1', '[::1]'].includes(location.hostname);
      box.replaceChildren(
        h('div', { class: 'section-title', text: 'You and Sparx' }),
        field('Assistant name', text('name', s.name)), field('Your name', text('user', s.user)),
        field('Addresses you as', text('address', s.address)),
        field('Personality', pick('personality', s.personality, ['sparx', 'casual', 'professional', 'technical'])),
        field('Reply language', pick('language', s.language, ['en', 'hi', 'kn'])),
        h('div', { class: 'section-title', text: 'Proactive behaviour' }), ...auto('proactive'),
        h('div', { class: 'section-title', text: 'Voice (the desktop voice loop, `sparx voice`)' }), ...auto('voice'),
        h('div', { class: 'section-title', text: 'Models (empty = the default)' }), suggest,
        h('p', { class: 'hint', text: Object.entries(found.providers).map(([p, n]) => `${p}: ${n}`).join(' · ') || 'No model providers found.' }),
        ...auto('models'),
        h('div', { class: 'section-title', text: 'Permissions: what Sparx may do without asking' }),
        h('p', { class: 'hint', text: d.pin_set ? 'Enter your PIN, then change a level.'
          : 'Read-only here. To change them from the browser, set a PIN on the computer first: python -m sparx.auth set-pin' }),
        d.pin_set && field('PIN', pin), ...perms,
        h('div', { class: 'section-title', text: 'Devices and session' }),
        h('div', { class: 'card-actions' },
          h('button', { class: 'btn small', type: 'button', text: 'Link another device', onclick: async () => {
            try {
              const r = await api('/api/auth/code', { method: 'POST' });
              linkBox.hidden = false;
              linkBox.replaceChildren(h('div', { text: `Open this on the other device within ${Math.round(r.expires_in / 60)} minutes. It works once.` }),
                h('div', { class: 'linkbox', text: location.origin + r.path }),
                local && h('div', { class: 'card-why', text: 'This address only works on this computer. For a phone, start Sparx with `sparx serve --host 0.0.0.0` (same Wi-Fi, unencrypted) or, better, reach it through Tailscale or an HTTPS tunnel, and use that address instead.' }));
            } catch (e) { oops(e); }
          } }),
          d.pin_set && h('button', { class: 'btn small', type: 'button', text: 'Lock now', onclick: async () => {
            try { await api('/api/lock', { method: 'POST' }); gate('pin', ''); } catch (e) { oops(e); }
          } }),
          h('button', { class: 'btn small danger', type: 'button', text: 'Sign out', onclick: logout })),
        linkBox,
        h('p', { class: 'hint', text: 'Limits, allowed folders, integrations and the server address can only be changed on the computer with `sparx config`.' }));
    },
  };

  async function show(view) {
    S.view = view;
    document.body.dataset.view = view;
    $$('.rail button').forEach((b) => b.classList.toggle('on', b.dataset.nav === view));
    $$('main .view').forEach((v) => v.classList.toggle('on', v.dataset.view === view));
    if (view === 'chat') { scrollDown(); return; }
    if (view === 'systems') { refreshState(); pollVitals(); return; }
    const box = $(`#${view}-body`);
    if (!box.children.length) box.replaceChildren(empty('Loading…'));
    try { await PANELS[view](box); } catch (e) { box.replaceChildren(empty(`Could not load this: ${e.message}`)); }
    if (view === 'alerts') bumpUnread(0);
  }

  $('.rail').addEventListener('click', (e) => { const b = e.target.closest('button[data-nav]'); if (b) show(b.dataset.nav); });
  $('#dash-kind').addEventListener('change', () => show('tasks'));
  $('#dash-open').addEventListener('change', () => show('tasks'));
  $('#memory-all').addEventListener('change', () => show('memory'));
  $('#alerts-read').addEventListener('click', async () => {
    try { await api('/api/notifications/read', { method: 'POST', body: {} }); S.state.unread = 0; show('alerts'); } catch (e) { oops(e); }
  });
  $('#conv-new').addEventListener('click', async () => {
    try { S.state.conversation = (await api('/api/conversations', { method: 'POST' })).current; await loadMessages(); show('chat'); input.focus(); }
    catch (e) { oops(e); }
  });
  document.addEventListener('visibilitychange', () => { if (!document.hidden && S.state) pollVitals(); });
  matchMedia('(max-width: 1100px)').addEventListener('change', (m) => { if (!m.matches && S.view === 'systems') show('chat'); });

  // ---------------------------------------------------------------- start

  let voiceReady = false, bootTimer = 0;
  async function boot() {
    clearTimeout(bootTimer);
    try { S.state = await api('/api/state'); } catch (e) {
      if (e.status === 401 || e.status === 423) return;      // api() has put up the sign-in or PIN screen
      gate('login', `${e.message} Is \`sparx serve\` running? Trying again…`);
      bootTimer = setTimeout(boot, 5000);
      return;
    }
    $('#gate').hidden = true;
    $('#app').hidden = false;
    S.stopped = !!S.state.stop;
    renderSparx();
    if (!voiceReady) { voiceReady = true; setupVoice(); }
    setCore();
    connect();
    show(S.view);
    loadGreeting();
    loadProtocols();
    loadMessages();
    pollVitals();
  }

  if (location.hash === '#login-failed') {
    history.replaceState(null, '', '/');
    $('#gate-msg').textContent = 'That sign-in link has expired or was already used. Paste your access token, or get a new link from `sparx serve`.';
  }
  if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => { /* not a secure context */ });
  boot();
})();
