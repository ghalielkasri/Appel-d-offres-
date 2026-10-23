/* Radar: ⌘K search, smooth area charts, mobile menu. Pages work without it. */
(() => {
  const nf = new Intl.NumberFormat('fr-FR', { maximumFractionDigits: 0 });
  const esc = (s) => String(s ?? '').replace(/[&<>"']/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const money = (v) => (v == null ? '—' : `${nf.format(v)} DH`);

  // ── Mobile menu ──────────────────────────────────────────────────────────
  document.addEventListener('click', (e) => {
    if (e.target.closest('[data-nav-toggle]')) document.body.classList.toggle('nav-open');
    else if (document.body.classList.contains('nav-open') && !e.target.closest('#side')) document.body.classList.remove('nav-open');
  });

  // ── Auto-submit: a select or radio that changes its form ─────────────────
  document.addEventListener('change', (e) => {
    if (e.target.matches('[data-autosubmit]')) e.target.form.requestSubmit();
  });

  // ── ⌘K palette ───────────────────────────────────────────────────────────
  const box = document.querySelector('[data-cmdk]');
  if (box) {
    const input = box.querySelector('[data-cmdk-input]');
    const list = box.querySelector('[data-cmdk-list]');
    let items = [];
    let cursor = -1;
    let timer = null;
    let seq = 0;

    const open = () => { box.hidden = false; input.value = ''; render([], ''); input.focus(); };
    const close = () => { box.hidden = true; };
    const mark = () => list.querySelectorAll('.rd-cmdk-i').forEach((el, i) => el.classList.toggle('kb', i === cursor));

    const render = (groups, q) => {
      items = [];
      if (q.length < 2) { list.innerHTML = '<div class="rd-cmdk-empty">Tapez au moins deux lettres. Les accents sont inutiles.</div>'; return; }
      const html = groups.filter((g) => g.rows.length).map((g) => `<div class="rd-cmdk-h">${esc(g.title)}</div>` + g.rows.map((r) => {
        items.push(r.href);
        return `<a class="rd-cmdk-i" href="${esc(r.href)}"><span class="n">${esc(r.name)}</span><span class="s">${esc(r.sub)}</span></a>`;
      }).join('')).join('');
      list.innerHTML = html || `<div class="rd-cmdk-empty">Aucun résultat pour « ${esc(q)} ».</div>`;
      cursor = items.length ? 0 : -1;
      mark();
    };

    const search = async (q) => {
      const mine = ++seq;
      try {
        const res = await fetch(`/api/search?limit=5&q=${encodeURIComponent(q)}`, { headers: { Accept: 'application/json' } });
        if (!res.ok || mine !== seq) return;
        const data = await res.json();
        render([
          { title: "Appels d'offres", rows: data.tenders.items.map((t) => ({ href: `/avis/${t.id}`, name: t.object || t.reference || 'Avis', sub: t.buyer || '' })) },
          { title: 'Attributaires', rows: data.suppliers.filter((s) => s.supplier_id).map((s) => ({ href: `/attributaires/${s.supplier_id}`, name: s.name, sub: `${s.awards_count} marché(s)` })) },
          { title: 'Acheteurs', rows: data.buyers.filter((b) => b.buyer_id).map((b) => ({ href: `/acheteurs/${b.buyer_id}`, name: b.name, sub: money(b.total_amount) })) },
          { title: 'Attributions', rows: data.awards.items.map((a) => ({ href: `/recherche?q=${encodeURIComponent(q)}`, name: a.object || a.reference || 'Attribution', sub: `${a.supplier || '—'} · ${money(a.awarded_amount)}` })) },
        ], q);
      } catch (_) { /* reseau coupe : la recherche complete reste accessible par Entree */ }
    };

    document.addEventListener('keydown', (e) => {
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === 'k') { e.preventDefault(); box.hidden ? open() : close(); }
      else if (e.key === '/' && box.hidden && !e.target.closest('input,textarea,select')) { e.preventDefault(); open(); }
      else if (!box.hidden && e.key === 'Escape') close();
    });
    document.addEventListener('click', (e) => {
      if (e.target.closest('[data-cmdk-open]')) open();
      else if (e.target === box) close();
    });
    input.addEventListener('input', () => {
      const q = input.value.trim();
      clearTimeout(timer);
      if (q.length < 2) { render([], q); return; }
      timer = setTimeout(() => search(q), 160);
    });
    input.addEventListener('keydown', (e) => {
      if (e.key === 'ArrowDown' && items.length) { e.preventDefault(); cursor = (cursor + 1) % items.length; mark(); }
      else if (e.key === 'ArrowUp' && items.length) { e.preventDefault(); cursor = (cursor - 1 + items.length) % items.length; mark(); }
      else if (e.key === 'Enter') {
        e.preventDefault();
        const q = input.value.trim();
        if (cursor >= 0 && items[cursor]) window.location.href = items[cursor];
        else if (q) window.location.href = `/recherche?q=${encodeURIComponent(q)}`;
      }
    });
  }

  // ── Area chart: monotone line over a lime glow, with a hover tooltip ─────
  const NS = 'http://www.w3.org/2000/svg';
  const smooth = (pts) => {
    // Monotone cubic (Fritsch-Carlson): no overshoot below zero between points.
    const n = pts.length;
    if (n < 2) return `M${pts[0][0]},${pts[0][1]}`;
    const dx = [], m = [], t = [];
    for (let i = 0; i < n - 1; i++) { dx[i] = pts[i + 1][0] - pts[i][0]; m[i] = (pts[i + 1][1] - pts[i][1]) / dx[i]; }
    t[0] = m[0]; t[n - 1] = m[n - 2];
    for (let i = 1; i < n - 1; i++) t[i] = m[i - 1] * m[i] <= 0 ? 0 : (3 * (dx[i - 1] + dx[i])) / ((2 * dx[i] + dx[i - 1]) / m[i - 1] + (dx[i] + 2 * dx[i - 1]) / m[i]);
    let d = `M${pts[0][0]},${pts[0][1]}`;
    for (let i = 0; i < n - 1; i++) {
      const h = dx[i] / 3;
      d += `C${pts[i][0] + h},${pts[i][1] + h * t[i]} ${pts[i + 1][0] - h},${pts[i + 1][1] - h * t[i + 1]} ${pts[i + 1][0]},${pts[i + 1][1]}`;
    }
    return d;
  };

  document.querySelectorAll('[data-chart]').forEach((el, idx) => {
    let series;
    try { series = JSON.parse(el.dataset.chart); } catch (_) { return; }
    if (!series.length) return;
    const W = 1000, H = 100, pad = 6;
    const max = Math.max(...series.map((p) => p.total), 1);
    const x = (i) => (series.length === 1 ? W / 2 : (i * W) / (series.length - 1));
    const y = (v) => H - pad - (v / max) * (H - pad * 2);
    const pts = series.map((p, i) => [x(i), y(p.total)]);
    const line = smooth(pts);
    const gid = `rdg${idx}`;
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.innerHTML = `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--ct-accent);stop-opacity:.55"/><stop offset="1" style="stop-color:var(--ct-accent);stop-opacity:0"/></linearGradient></defs>`
      + [0.25, 0.5, 0.75].map((f) => `<line class="ct-grid" x1="0" x2="${W}" y1="${H * f}" y2="${H * f}"/>`).join('')
      + `<path d="${line} L${W},${H} L0,${H} Z" fill="url(#${gid})" stroke="none"/><path class="ct-line" d="${line}"/>`;
    el.appendChild(svg);

    const cur = document.createElement('div'); cur.className = 'ct-cursor';
    const dot = document.createElement('div'); dot.className = 'ct-dot';
    const tip = document.createElement('div'); tip.className = 'ct-tip';
    el.append(cur, dot, tip);
    const vl = el.dataset.valueLabel || 'Montant';
    const cl = el.dataset.countLabel || 'Marchés';

    el.addEventListener('mousemove', (e) => {
      const r = el.getBoundingClientRect();
      const i = Math.max(0, Math.min(series.length - 1, Math.round(((e.clientX - r.left) / r.width) * (series.length - 1))));
      const p = series[i];
      const px = (x(i) / W) * r.width, py = (y(p.total) / H) * r.height;
      cur.style.left = `${px}px`; dot.style.left = `${px}px`; dot.style.top = `${py}px`;
      tip.style.left = `${px}px`;
      tip.classList.toggle('flip', px > r.width * 0.6);
      tip.innerHTML = `<div class="ct-tip-t">${esc(p.label)}</div><div class="ct-tip-r"><i></i>${esc(vl)}<b>${money(p.total)}</b></div><div class="ct-tip-r"><i style="background:var(--ct-border-strong)"></i>${esc(cl)}<b>${nf.format(p.count)}</b></div>`;
      el.classList.add('hover');
    });
    el.addEventListener('mouseleave', () => el.classList.remove('hover'));
  });

  // ── Multi-line chart: one line per company, the first one over the glow ──
  document.querySelectorAll('[data-chart-multi]').forEach((el, idx) => {
    let data;
    try { data = JSON.parse(el.dataset.chartMulti); } catch (_) { return; }
    const { labels, series } = data;
    if (!labels || !labels.length || !series || !series.length) return;
    const W = 1000, H = 100, pad = 6;
    const max = Math.max(1, ...series.flatMap((s) => s.values));
    const x = (i) => (labels.length === 1 ? W / 2 : (i * W) / (labels.length - 1));
    const y = (v) => H - pad - (v / max) * (H - pad * 2);
    const gid = `rdm${idx}`;
    const lines = series.map((s) => smooth(s.values.map((v, i) => [x(i), y(v)])));
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`);
    svg.setAttribute('preserveAspectRatio', 'none');
    svg.innerHTML = `<defs><linearGradient id="${gid}" x1="0" y1="0" x2="0" y2="1"><stop offset="0" style="stop-color:var(--ct-accent);stop-opacity:.5"/><stop offset="1" style="stop-color:var(--ct-accent);stop-opacity:0"/></linearGradient></defs>`
      + [0.25, 0.5, 0.75].map((f) => `<line class="ct-grid" x1="0" x2="${W}" y1="${H * f}" y2="${H * f}"/>`).join('')
      + `<path d="${lines[0]} L${W},${H} L0,${H} Z" fill="url(#${gid})" stroke="none"/>`
      + lines.map((d, i) => `<path class="ct-line ct-s${i + 1}" d="${d}"/>`).join('');
    el.appendChild(svg);
    const cur = document.createElement('div'); cur.className = 'ct-cursor';
    const dots = series.map((_, i) => { const d = document.createElement('div'); d.className = `ct-dot ct-s${i + 1}`; return d; });
    const tip = document.createElement('div'); tip.className = 'ct-tip';
    el.append(cur, ...dots, tip);
    el.addEventListener('mousemove', (e) => {
      const r = el.getBoundingClientRect();
      const i = Math.max(0, Math.min(labels.length - 1, Math.round(((e.clientX - r.left) / r.width) * (labels.length - 1))));
      const px = (x(i) / W) * r.width;
      cur.style.left = `${px}px`;
      dots.forEach((d, k) => { d.style.left = `${px}px`; d.style.top = `${(y(series[k].values[i]) / H) * r.height}px`; });
      tip.style.left = `${px}px`;
      tip.classList.toggle('flip', px > r.width * 0.6);
      tip.innerHTML = `<div class="ct-tip-t">${esc(labels[i])}</div>` + series.map((s, k) => `<div class="ct-tip-r"><i class="s${k + 1}"></i>${esc(s.name)}<b>${money(s.values[i])}</b></div>`).join('');
      el.classList.add('hover');
    });
    el.addEventListener('mouseleave', () => el.classList.remove('hover'));
  });
})();
