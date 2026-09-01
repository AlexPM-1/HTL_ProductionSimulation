/* movement_layout.js
 *
 * Shared engine for the Movement Simulation page (movement_simulation.html):
 * station-track SVG layout, segment interpolation (piece/card position at
 * an arbitrary time), and scrub/playback controls.
 *
 * Color helpers and escapeHtml/unitLabel are copied verbatim from
 * kanban_production_planner_dashboard.html so both pages render
 * consistently without pulling in the whole 2,150-line dashboard file.
 *
 * No build step — plain global, loaded via <script src="movement_layout.js">.
 */
(function (global) {
  'use strict';

  // ===========================================================================
  // Copied from the dashboard (kept byte-identical on purpose — see
  // kanban_production_planner_dashboard.html's own copies around line 648).
  // ===========================================================================
  const COLOR_PALETTE = ['#2563EB', '#F59E0B', '#F97316', '#7C3AED', '#0D9488', '#DB2777', '#16A34A', '#4F46E5', '#0EA5E9', '#CA8A04', '#65A30D', '#C026D3'];

  function hexToHue(hex) {
    const r = parseInt(hex.slice(1, 3), 16) / 255;
    const g = parseInt(hex.slice(3, 5), 16) / 255;
    const b = parseInt(hex.slice(5, 7), 16) / 255;
    const max = Math.max(r, g, b), min = Math.min(r, g, b), d = max - min;
    if (d === 0) return 0;
    let h;
    if (max === r) h = ((g - b) / d) % 6;
    else if (max === g) h = (b - r) / d + 2;
    else h = (r - g) / d + 4;
    h *= 60;
    return h < 0 ? h + 360 : h;
  }
  const PALETTE_HUES = COLOR_PALETTE.map(hexToHue);
  const MIN_HUE_SEPARATION = 25;
  let generatedHues = [];
  function circularHueDist(a, b) {
    const diff = Math.abs(a - b) % 360;
    return diff > 180 ? 360 - diff : diff;
  }
  function generateColor(idx) {
    if (idx < COLOR_PALETTE.length) return COLOR_PALETTE[idx];
    let hue = ((idx - COLOR_PALETTE.length) * 137.508) % 360;
    let attempts = 0;
    const allHues = () => PALETTE_HUES.concat(generatedHues);
    while (allHues().some(h => circularHueDist(hue, h) < MIN_HUE_SEPARATION) && attempts < 72) {
      hue = (hue + 5) % 360;
      attempts++;
    }
    generatedHues.push(hue);
    return `hsl(${hue.toFixed(0)}, 62%, 45%)`;
  }
  function escapeHtml(s) {
    const d = document.createElement('div');
    d.innerText = String(s);
    return d.innerHTML;
  }
  function unitLabel(u) { return u === 'h' ? 'h' : (u === 'min' ? 'm' : 's'); }

  // How much extra to run the slider/track past the last recorded
  // station-exit or transition, in the trace's own time_unit. Without
  // this, playback stops the instant the piece/card reaches its last
  // recorded point, which reads as the movement being cut off before
  // it visibly "finishes". ~8 raw seconds (mid of the requested 5-10s
  // window) converted into whatever unit the trace is in.
  const _END_PADDING_RAW_SECONDS = 8;
  const _DIVISORS_LOCAL = { h: 3600, min: 60, s: 1 };
  function trackEndPadding(unit) {
    const divisor = _DIVISORS_LOCAL[unit] || 1;
    return _END_PADDING_RAW_SECONDS / divisor;
  }

  // Raw seconds (as returned by the trace endpoints when time_unit='s',
  // already includes push's day-stitching offset — multiples of 86400,
  // which vanish under the mod below) -> "HH:MM:SS" clock-of-day.
  // dayStartHour: the run's day_start_hour / shift_start_hour (t=0 of
  // day_idx 0 == that many hours past midnight) — comes back on every
  // trace response so the caller never has to hardcode it.
  function formatClock(tSeconds, dayStartHour) {
    if (tSeconds == null || isNaN(tSeconds)) return '--:--:--';
    const dsh = dayStartHour == null ? 0 : dayStartHour;
    let total = Math.floor(tSeconds + dsh * 3600);
    total = ((total % 86400) + 86400) % 86400;
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const pad = (n) => String(n).padStart(2, '0');
    return `${pad(h)}:${pad(m)}:${pad(s)}`;
  }

  // Same raw-seconds input as formatClock, but prefixed with a "Day N"
  // label (1-indexed, matching the UI's 1-indexed day inputs) — there's
  // no real calendar date in a simulated run, so the day count within
  // the run is the closest equivalent. Used for card transitions, which
  // (unlike a single day/hour of piece data) can span several days.
  function formatDateTime(tSeconds, dayStartHour) {
    if (tSeconds == null || isNaN(tSeconds)) return '--';
    const dsh = dayStartHour == null ? 0 : dayStartHour;
    const shifted = Math.max(tSeconds + dsh * 3600, 0);
    const dayIdx = Math.floor(shifted / 86400); // 0-indexed, same bucketing formatClock wraps on
    return `Day ${dayIdx + 1}, ${formatClock(tSeconds, dayStartHour)}`;
  }

  // Inverse of formatDateTime()/formatClock(): turns a "Day N, HH:MM:SS"
  // (or bare "HH:MM:SS", defaulting to day 1) string back into raw
  // tSeconds — the same absolute, day-stitched domain formatClock and
  // formatDateTime take (see formatClock's docstring: multiples of
  // 86400 baked into t, not reset per day). Returns null if the string
  // doesn't parse or the day/time is out of range, so callers can leave
  // the current value in place on a bad entry instead of jumping to a
  // garbage time.
  function parseDateTimeInput(str, dayStartHour) {
    if (!str) return null;
    const m = String(str).trim().match(/^(?:Day\s*(\d+)\s*,?\s*)?(\d{1,2}):(\d{2})(?::(\d{2}))?$/i);
    if (!m) return null;
    const dayN = m[1] ? parseInt(m[1], 10) : 1;
    const hh = parseInt(m[2], 10), mm = parseInt(m[3], 10), ss = m[4] ? parseInt(m[4], 10) : 0;
    if (dayN < 1 || hh > 23 || mm > 59 || ss > 59) return null;
    const dsh = dayStartHour == null ? 0 : dayStartHour;
    const clockSeconds = hh * 3600 + mm * 60 + ss;
    // Inverse of formatClock: total = floor(tSeconds + dsh*3600), then
    // wrapped = ((total % 86400) + 86400) % 86400. So, picking the
    // representative tSeconds that lands in day (dayN-1) with that
    // clock-of-day: tSeconds = (dayN-1)*86400 + clockSeconds - dsh*3600.
    return (dayN - 1) * 86400 + clockSeconds - dsh * 3600;
  }

  // Both renderStationTrack() and renderPlantLayout() draw at their normal,
  // unscaled coordinates below, then get wrapped in a single <g scale(...)>
  // group and a matching (shrunk) viewBox — see _startScaledSvg(). That's
  // a uniform "zoom out" of the whole drawing (like the browser's own
  // page zoom, just baked into the SVG) so it takes noticeably less
  // screen space without having to re-derive every box/gap/font-size
  // constant below by hand. Card layers added later (renderPlantCards)
  // are appended into the SAME scaled group (see its _scaleRootOf use)
  // so dynamically-placed cards line up with the statically-drawn slots.
  const DIAGRAM_SCALE = 0.85;

  // Clears svgEl, sets its viewBox to the shrunk (totalW*scale x
  // totalH*scale) size, and returns a <g transform="scale(...)"> that all
  // drawing calls should append into instead of svgEl directly. Content
  // inside keeps using the original, unscaled coordinates.
  function _startScaledSvg(svgEl, totalW, totalH, scale) {
    svgEl.setAttribute('viewBox', `0 0 ${totalW * scale} ${totalH * scale}`);
    svgEl.innerHTML = '';
    const root = _ns2('g');
    root.setAttribute('id', 'diagramScaleRoot');
    root.setAttribute('transform', `scale(${scale})`);
    svgEl.appendChild(root);
    return root;
  }

  // Finds the scale wrapper a previous _startScaledSvg() call created on
  // this svgEl, so a later card/token layer can be appended inside it
  // (and therefore inherit the same scale) instead of at the svg root.
  // Falls back to svgEl itself for any layout drawn before this existed.
  function _scaleRootOf(svgEl) {
    return svgEl.querySelector('#diagramScaleRoot') || svgEl;
  }

  // (Note: _ns2(), a tiny createElementNS('svg', tag) helper identical to
  // this file's older _ns(), is defined further below near renderPlantCards
  // and reused here too — not redefined, to avoid two copies drifting.)

  // ===========================================================================
  // Station track — draws N boxes in a row, connected by a dashed line,
  // and one marker (dot) whose position is updated via positionAtTime().
  // ===========================================================================
  function renderStationTrack(svgEl, stationLabels, opts) {
    opts = opts || {};
    const boxW = opts.boxW || 130, boxH = opts.boxH || 58;
    const gap = opts.gap || 56;
    const padX = opts.padX || 30, padY = opts.padY || 44;
    const n = Math.max(stationLabels.length, 1);
    const totalW = padX * 2 + n * boxW + (n - 1) * gap;
    const totalH = padY * 2 + boxH;
    const root = _startScaledSvg(svgEl, totalW, totalH, DIAGRAM_SCALE);

    const positions = {};
    const ns = 'http://www.w3.org/2000/svg';
    const midY = padY + boxH / 2;

    const line = document.createElementNS(ns, 'line');
    line.setAttribute('x1', padX + boxW / 2);
    line.setAttribute('y1', midY);
    line.setAttribute('x2', totalW - padX - boxW / 2);
    line.setAttribute('y2', midY);
    line.setAttribute('stroke', '#D7DBE2');
    line.setAttribute('stroke-width', '2');
    line.setAttribute('stroke-dasharray', '4 4');
    root.appendChild(line);

    stationLabels.forEach((label, i) => {
      const x = padX + i * (boxW + gap);
      const y = padY;
      const rect = document.createElementNS(ns, 'rect');
      rect.setAttribute('x', x);
      rect.setAttribute('y', y);
      rect.setAttribute('width', boxW);
      rect.setAttribute('height', boxH);
      rect.setAttribute('rx', 10);
      rect.setAttribute('fill', '#FFFFFF');
      rect.setAttribute('stroke', '#E4E7EC');
      rect.setAttribute('stroke-width', '1.5');
      root.appendChild(rect);

      const text = document.createElementNS(ns, 'text');
      text.setAttribute('x', x + boxW / 2);
      text.setAttribute('y', y + boxH / 2 + 4);
      text.setAttribute('text-anchor', 'middle');
      text.setAttribute('font-size', '12');
      text.setAttribute('font-weight', '600');
      text.setAttribute('fill', '#1B2330');
      text.textContent = label;
      root.appendChild(text);

      positions[label] = { x: x + boxW / 2, y: y + boxH / 2 };
    });

    const marker = document.createElementNS(ns, 'circle');
    marker.setAttribute('r', 9);
    marker.setAttribute('fill', '#2563EB');
    marker.setAttribute('stroke', '#fff');
    marker.setAttribute('stroke-width', '2');
    marker.style.filter = 'drop-shadow(0 1px 2px rgba(16,24,40,.25))';
    root.appendChild(marker);

    return { positions, marker, width: totalW, height: totalH };
  }

  // ===========================================================================
  // Plant layout — spatial diagram of one line's Kanban loop:
  //
  //   Supermarket --(card withdrawn)--> Collection Box --> Batch Collector
  //   --(trigger reached)--> Chute (frozen zone at the bottom) --> Production
  //   --> back to Supermarket
  //
  // This is a DIFFERENT view from renderStationTrack() above: that one is a
  // single entity's linear timeline; this one is a static physical-plant map
  // that every card for a line shares. Step 1 (this function) only draws the
  // shape and returns where each slot/queue-position/anchor sits, in SVG
  // user-space coordinates — it draws no cards. Step 3 (later) will use the
  // returned `positions` to place/animate card dots without needing to know
  // any layout math again.
  //
  // Component -> real-world meaning (per user spec):
  //   collectionBox   : simple pool, no fixed slots — cards deposited here
  //                      after a supermarket withdrawal frees them.
  //   batchCollector  : one row per pull product, cards held until
  //                      row.triggerAmount is reached, then released as a
  //                      group to the chute. Rows are NOT the same as
  //                      Supermarket rows (no Exoten rows here).
  //   chute           : tapered queue ("triangle"), capacity cards deep,
  //                      with a frozenZone-sized band at the production end
  //                      that's locked (no reordering) once cards enter it.
  //   supermarket     : one row per physical lane, `slots` cards per row.
  // ===========================================================================

  const PLANT_COLORS = {
    boxFill: '#FFFFFF',
    boxFillMuted: '#FAFBFD',
    boxStroke: '#E4E7EC',
    text: '#1B2330',
    textMuted: '#6B7280',
    textFaint: '#9CA3AF',
    blue: '#2563EB',
    blueLight: '#EAF1FF',
    amber: '#B45309',
    amberLight: '#FDF3E7',
    red: '#DC2626',
    redLight: '#FEE2E2',
    slotEmpty: '#C6CBD3',
    flowLine: '#9CA3AF',
  };

  const DEFAULT_PLANT_CONFIG = {
    lineLabel: 'Line 1',
    collectionBox: { poolHint: 8 },
    batchCollector: {
      rows: [
        { product: 'Product A', capacity: 12, triggerAmount: 4 },
        { product: 'Product B', capacity: 12, triggerAmount: 4 },
        { product: 'Product C', capacity: 12, triggerAmount: 6 },
      ],
    },
    chute: { capacity: 20, frozenZone: 8 },
    production: { poolHint: 6 },
    supermarket: {
      rows: [
        { label: 'Row 1', slots: 12 },
        { label: 'Row 2', slots: 12 },
        { label: 'Row 3', slots: 10 },
        { label: 'Row 4', slots: 10 },
        { label: 'Row 5', slots: 12 },
      ],
    },
  };

  // Exotic supermarket rows don't have a hard physical stop the way the
  // drawn capacity implies — a row that hasn't fully emptied still gets
  // filled by whatever's currently being produced. Rather than let that
  // overflow disappear into a "+N" badge only, every Exotic row gets this
  // many extra slots drawn (visually offset + amber/dashed) so overflow is
  // directly visible as real occupied slots, not just a number.
  const EXOTIC_OVERFLOW_SLOTS = 5;

  function _ns(tag) { return document.createElementNS('http://www.w3.org/2000/svg', tag); }

  function _svgRect(x, y, w, h, opts) {
    opts = opts || {};
    const r = _ns('rect');
    r.setAttribute('x', x); r.setAttribute('y', y);
    r.setAttribute('width', w); r.setAttribute('height', h);
    r.setAttribute('rx', opts.rx != null ? opts.rx : 10);
    r.setAttribute('fill', opts.fill || PLANT_COLORS.boxFill);
    r.setAttribute('stroke', opts.stroke || PLANT_COLORS.boxStroke);
    r.setAttribute('stroke-width', opts.strokeWidth || 1.5);
    if (opts.dash) r.setAttribute('stroke-dasharray', opts.dash);
    return r;
  }

  function _svgText(x, y, str, opts) {
    opts = opts || {};
    const t = _ns('text');
    t.setAttribute('x', x); t.setAttribute('y', y);
    t.setAttribute('text-anchor', opts.anchor || 'start');
    t.setAttribute('font-size', opts.size || 12);
    t.setAttribute('font-weight', opts.weight || 400);
    t.setAttribute('fill', opts.fill || PLANT_COLORS.text);
    if (opts.letterSpacing) t.setAttribute('letter-spacing', opts.letterSpacing);
    t.textContent = str;
    return t;
  }

  // For the vertical "card withdrawn" label running up the left margin.
  function _svgTextRotated(x, y, str, angleDeg, opts) {
    const t = _svgText(0, 0, str, Object.assign({ anchor: 'middle' }, opts || {}));
    t.setAttribute('transform', `translate(${x},${y}) rotate(${angleDeg})`);
    return t;
  }

  function _svgLine(x1, y1, x2, y2, opts) {
    opts = opts || {};
    const l = _ns('line');
    l.setAttribute('x1', x1); l.setAttribute('y1', y1);
    l.setAttribute('x2', x2); l.setAttribute('y2', y2);
    l.setAttribute('stroke', opts.stroke || PLANT_COLORS.flowLine);
    l.setAttribute('stroke-width', opts.width || 2);
    if (opts.dash) l.setAttribute('stroke-dasharray', opts.dash);
    if (opts.marker) l.setAttribute('marker-end', `url(#${opts.marker === 'blue' ? 'plantArrowHeadBlue' : 'plantArrowHead'})`);
    return l;
  }

  function _svgPath(d, opts) {
    opts = opts || {};
    const p = _ns('path');
    p.setAttribute('d', d);
    p.setAttribute('fill', opts.fill || 'none');
    p.setAttribute('stroke', opts.stroke || PLANT_COLORS.flowLine);
    p.setAttribute('stroke-width', opts.width || 2);
    if (opts.dash) p.setAttribute('stroke-dasharray', opts.dash);
    if (opts.marker) p.setAttribute('marker-end', `url(#${opts.marker === 'blue' ? 'plantArrowHeadBlue' : 'plantArrowHead'})`);
    return p;
  }

  // A single placeholder slot cell — empty/dashed here in Step 1; Step 3
  // fills these with a solid, colored rect (+ card id) when occupied.
  function _slotCell(x, y, size, opts) {
    opts = opts || {};
    const rect = _ns('rect');
    rect.setAttribute('x', x); rect.setAttribute('y', y);
    rect.setAttribute('width', size); rect.setAttribute('height', size);
    rect.setAttribute('rx', 3);
    rect.setAttribute('fill', opts.fill || PLANT_COLORS.boxFillMuted);
    rect.setAttribute('stroke', opts.stroke || PLANT_COLORS.slotEmpty);
    rect.setAttribute('stroke-width', 1.25);
    if (opts.dash) rect.setAttribute('stroke-dasharray', opts.dash);
    return rect;
  }

  function _plantArrowDefs(svgEl) {
    const defs = _ns('defs');
    const mk = (id, color) => {
      const marker = _ns('marker');
      marker.setAttribute('id', id);
      marker.setAttribute('viewBox', '0 0 10 10');
      marker.setAttribute('refX', '8'); marker.setAttribute('refY', '5');
      marker.setAttribute('markerWidth', '7'); marker.setAttribute('markerHeight', '7');
      marker.setAttribute('orient', 'auto-start-reverse');
      const path = _ns('path');
      path.setAttribute('d', 'M0,0 L10,5 L0,10 Z');
      path.setAttribute('fill', color);
      marker.appendChild(path);
      defs.appendChild(marker);
    };
    mk('plantArrowHead', PLANT_COLORS.flowLine);
    mk('plantArrowHeadBlue', PLANT_COLORS.blue);
    svgEl.appendChild(defs);
  }

  // Renders the full static plant map into svgEl and returns every
  // slot/queue-position/anchor point it drew, keyed for Step 3 to consume:
  //   { positions: {
  //       collectionBox: {x,y},          // center point (anchor use)
  //       collectionBoxBox: {x,y,w,h},   // raw box rect (legend layout use)
  //       batchCollector: [{product, triggerAmount, slots:[{x,y}...]}...],
  //       chute: [{x,y,frozen}...],           // index 0 = entry (top), last = production end
  //       production: {x,y},                  // rectangle center (label/anchor use)
  //       productionBox: {x,y,w,h},           // raw box rect, for the dynamic status label
  //       productionSlots: [{x,y}...],         // dashed per-card placeholder slots
  //       supermarket: [{label, slots:[{x,y,overflow}...]}...],  // slots past
  //                                             // normal capacity on Exotic rows
  //                                             // are tagged overflow:true
  //       restmengeAnchors: {sachnummer: {x,y,batchSize}},  // one live-badge
  //                                             // spot per Main-runner product,
  //                                             // only present when config.restmenge
  //                                             // was supplied
  //     },
  //     width, height }
  function renderPlantLayout(svgEl, config) {
    config = Object.assign({}, DEFAULT_PLANT_CONFIG, config || {});
    config.collectionBox = Object.assign({}, DEFAULT_PLANT_CONFIG.collectionBox, config.collectionBox || {});
    config.batchCollector = Object.assign({}, DEFAULT_PLANT_CONFIG.batchCollector, config.batchCollector || {});
    config.chute = Object.assign({}, DEFAULT_PLANT_CONFIG.chute, config.chute || {});
    config.production = Object.assign({}, DEFAULT_PLANT_CONFIG.production, config.production || {});
    config.supermarket = Object.assign({}, DEFAULT_PLANT_CONFIG.supermarket, config.supermarket || {});

    const pad = 30;
    const rowGap = 20;
    const colGap = 30;
    const slotSize = 20, slotGap = 6;

    // ---- Row 1 geometry: Collection Box | Batch Collector | Chute ------
    const cbW = 178, cbH = 132;
    const bcHeaderH = 34, bcRowH = 34, bcPadY = 14;
    const bcRows = config.batchCollector.rows.length ? config.batchCollector.rows : [{ product: '\u2014', capacity: 1, triggerAmount: 1 }];
    const bcMaxCap = Math.max(1, ...bcRows.map(r => r.capacity));
    const bcW = 96 + bcMaxCap * (slotSize + slotGap);
    const bcH = Math.max(cbH, bcHeaderH + bcRows.length * bcRowH + bcPadY);
    const chuteW = 150;
    const chuteH = Math.max(cbH, bcH, 230);
    const row1H = Math.max(cbH, bcH, chuteH);

    const cbX = pad, cbY = pad + (row1H - cbH) / 2;
    const bcX = cbX + cbW + colGap, bcY = pad + (row1H - bcH) / 2;
    const chuteX = bcX + bcW + colGap, chuteY = pad;

    const rowTotalW = (chuteX + chuteW) - cbX;

    // ---- Row 2: Production ---------------------------------------------
    const prodY = pad + row1H + rowGap;
    const prodH = 76;
    const prodX = cbX, prodW = rowTotalW;

    // ---- Row 3: Supermarket ---------------------------------------------
    const smY = prodY + prodH + rowGap;
    const smRows = config.supermarket.rows.length ? config.supermarket.rows : [{ label: 'Row 1', slots: 1 }];
    const smRowH = slotSize + slotGap + 6;
    const smHeaderH = 30;
    const smLabelW = 96;
    const smH = smHeaderH + smRows.length * smRowH + 10;
    const smX = cbX;
    // Restmenge: a fixed-width column reserved at the right edge of the
    // Supermarket box, one live-updating badge per Main-runner product —
    // see renderPlantCards, which fills the actual (constantly changing)
    // pcs_partial number in here every frame using the anchor positions
    // stored below. Only reserved when the caller actually supplied
    // restmenge rows (config.restmenge — from plant_structure's static
    // "restmenge" field), so a layout with no Restmenge data draws
    // exactly as before.
    const restmengeRows = config.restmenge || [];
    const hasRestmenge = restmengeRows.length > 0;
    const restmengeColW = hasRestmenge ? 58 : 0;
    // Exotic rows draw EXOTIC_OVERFLOW_SLOTS extra cells past their normal
    // capacity (plus a small gap to separate the two visually) — size the
    // Supermarket box (and the overall canvas) to whichever is wider: the
    // row1 (Collection Box/Batch Collector/Chute) width, or the widest
    // Exotic row's actual slot content, so the overflow cells never get
    // clipped by a box sized only for the non-exotic rows.
    const smContentW = smLabelW + Math.max(1, ...smRows.map((r) => {
      const extra = r.isExotic ? EXOTIC_OVERFLOW_SLOTS : 0;
      const gapExtra = r.isExotic ? 10 : 0;
      return (r.slots + extra) * (slotSize + slotGap) + gapExtra;
    })) + 20 + restmengeColW;
    const smW = Math.max(rowTotalW, smContentW);

    const totalW = pad * 2 + Math.max(rowTotalW, smW);
    const totalH = smY + smH + pad;

    const root = _startScaledSvg(svgEl, totalW, totalH, DIAGRAM_SCALE);
    _plantArrowDefs(svgEl);

    const positions = {
      collectionBox: null, batchCollector: [], chute: [], production: null, supermarket: [],
      // Lookup indices alongside the display-ordered arrays above, so Step
      // 3's card layer (renderPlantCards) can match a movement_state row
      // to its slots by a stable key (row_number / product) instead of by
      // array index, which can drift if the two payloads ever sort rows
      // differently (plant_structure vs _build_movement_state_payload
      // build their row lists independently, even though today they
      // happen to use matching sort orders).
      collectionBoxSlots: [],
      supermarketByRowNumber: {},
      batchCollectorByProduct: {},
      transitAnchors: {},
      restmengeAnchors: {},
    };

    // ---- Collection Box ---------------------------------------------------
    root.appendChild(_svgRect(cbX, cbY, cbW, cbH, {}));
    root.appendChild(_svgText(cbX + 14, cbY + 24, 'COLLECTION BOX', { size: 11, weight: 700, letterSpacing: '.03em' }));
    root.appendChild(_svgText(cbX + 14, cbY + 40, 'cards freed at withdrawal', { size: 10.5, fill: PLANT_COLORS.textMuted }));
    {
      const poolN = config.collectionBox.poolHint || 8;
      const perRow = 4;
      const dotR = 6.5, dotGapX = 24, dotGapY = 22;
      const startX = cbX + 20, startY = cbY + 62;
      for (let i = 0; i < poolN; i++) {
        const cx = startX + (i % perRow) * dotGapX;
        const cy = startY + Math.floor(i / perRow) * dotGapY;
        const c = _ns('circle');
        c.setAttribute('cx', cx); c.setAttribute('cy', cy); c.setAttribute('r', dotR);
        c.setAttribute('fill', 'none'); c.setAttribute('stroke', PLANT_COLORS.slotEmpty);
        c.setAttribute('stroke-width', 1.25); c.setAttribute('stroke-dasharray', '2 2');
        root.appendChild(c);
        positions.collectionBoxSlots.push({ x: cx, y: cy });
      }
      positions.collectionBoxPoolHint = poolN;
    }
    positions.collectionBox = { x: cbX + cbW / 2, y: cbY + cbH / 2 };
    // Raw box rect (not just the center point above) so the dynamic card
    // layer (renderPlantCards) can lay out a per-product color+count
    // legend inside it without recomputing this geometry itself.
    positions.collectionBoxBox = { x: cbX, y: cbY, w: cbW, h: cbH };

    // ---- Batch Collector ---------------------------------------------------
    root.appendChild(_svgRect(bcX, bcY, bcW, bcH, {}));
    root.appendChild(_svgText(bcX + 14, bcY + 22, 'BATCH COLLECTOR', { size: 11, weight: 700, letterSpacing: '.03em' }));
    bcRows.forEach((row, ri) => {
      const ry = bcY + bcHeaderH + ri * bcRowH + 10;
      root.appendChild(_svgText(bcX + 14, ry + slotSize / 2 + 4, row.product, { size: 10.5, weight: 600, fill: PLANT_COLORS.textMuted }));
      const slotsStartX = bcX + 96;
      const rowSlots = [];
      for (let s = 0; s < row.capacity; s++) {
        const sx = slotsStartX + s * (slotSize + slotGap);
        root.appendChild(_slotCell(sx, ry, slotSize, s < row.triggerAmount ? { stroke: PLANT_COLORS.amber } : {}));
        rowSlots.push({ x: sx + slotSize / 2, y: ry + slotSize / 2 });
      }
      const trigX = slotsStartX + row.triggerAmount * (slotSize + slotGap) - slotGap / 2;
      root.appendChild(_svgLine(trigX, ry - 4, trigX, ry + slotSize + 4, { stroke: PLANT_COLORS.amber, width: 1.5, dash: '3 3' }));
      positions.batchCollector.push({ product: row.product, triggerAmount: row.triggerAmount, slots: rowSlots });
      positions.batchCollectorByProduct[row.product] = rowSlots;
    });

    // ---- Chute (tapered "triangle", frozen zone at the bottom) -----------
    {
      const taper = 26;
      const d = `M ${chuteX},${chuteY} L ${chuteX + chuteW},${chuteY} L ${chuteX + chuteW - taper},${chuteY + chuteH} L ${chuteX + taper},${chuteY + chuteH} Z`;
      root.appendChild(_svgPath(d, { fill: PLANT_COLORS.boxFill, stroke: PLANT_COLORS.boxStroke, width: 1.5 }));

      const frozenN = Math.min(config.chute.frozenZone, config.chute.capacity);
      const cap = Math.max(1, config.chute.capacity);
      const frozenH = chuteH * (frozenN / cap);
      const insetTop = taper * (frozenH / chuteH);
      const dFrozen = `M ${chuteX + insetTop},${chuteY + chuteH - frozenH} L ${chuteX + chuteW - insetTop},${chuteY + chuteH - frozenH} `
        + `L ${chuteX + chuteW - taper},${chuteY + chuteH} L ${chuteX + taper},${chuteY + chuteH} Z`;
      root.appendChild(_svgPath(dFrozen, { fill: PLANT_COLORS.amberLight, stroke: 'none' }));
      root.appendChild(_svgLine(
        chuteX + insetTop, chuteY + chuteH - frozenH,
        chuteX + chuteW - insetTop, chuteY + chuteH - frozenH,
        { stroke: PLANT_COLORS.amber, width: 1.5, dash: '3 3' }
      ));

      root.appendChild(_svgText(chuteX + chuteW / 2, chuteY + 22, 'CHUTE', { size: 11, weight: 700, anchor: 'middle', letterSpacing: '.03em' }));
      root.appendChild(_svgText(chuteX + chuteW / 2, chuteY + chuteH - frozenH / 2, 'Frozen zone', { size: 9.5, weight: 700, anchor: 'middle', fill: PLANT_COLORS.amber }));
      root.appendChild(_svgText(chuteX + chuteW / 2, chuteY + chuteH - frozenH / 2 + 13, `${frozenN} cards`, { size: 9, anchor: 'middle', fill: PLANT_COLORS.amber }));

      const chuteSlots = [];
      for (let i = 0; i < cap; i++) {
        const frac = (i + 0.5) / cap;
        chuteSlots.push({ x: chuteX + chuteW / 2, y: chuteY + frac * chuteH, frozen: i >= cap - frozenN });
      }
      positions.chute = chuteSlots;
      positions.chuteCapacity = cap;
      positions.chuteFrozenCount = frozenN;
    }

    // ---- Forward flow: Collection Box -> Batch Collector -> Chute --------
    root.appendChild(_svgLine(cbX + cbW, cbY + cbH / 2, bcX, bcY + bcH / 2, { marker: true }));
    root.appendChild(_svgLine(bcX + bcW, bcY + bcH / 2, chuteX, chuteY + chuteH * 0.15, { marker: true }));
    root.appendChild(_svgText((bcX + bcW + chuteX) / 2, chuteY - 10, 'trigger reached', { size: 9.5, anchor: 'middle', fill: PLANT_COLORS.textMuted }));

    // ---- Production Line ---------------------------------------------------
    root.appendChild(_svgRect(prodX, prodY, prodW, prodH, { fill: PLANT_COLORS.boxFillMuted }));
    root.appendChild(_svgText(prodX + 14, prodY + 20, `PRODUCTION LINE \u2014 ${config.lineLabel}`, { size: 11, weight: 700, letterSpacing: '.02em' }));
    root.appendChild(_svgLine(chuteX + chuteW / 2, chuteY + chuteH, prodX + prodW / 2, prodY, { marker: true }));
    positions.production = { x: prodX + prodW / 2, y: prodY + prodH / 2 };
    positions.productionBox = { x: prodX, y: prodY, w: prodW, h: prodH };
    positions.transitAnchors.in_production = positions.production;
    {
      // Dashed placeholder slots for the piece(s) actually in production
      // right now — filled in by renderPlantCards from
      // lineFrame.production_occupants (resolved server-side from
      // production_status — the single job the shared LinePriorityGate is
      // actually holding, NOT a raw in_transit/'in_production' count,
      // which can include cards from a batch that's queued but hasn't
      // started yet — see _build_movement_state_payload's docstring). A
      // released batch can legitimately have more than one card running
      // concurrently, so this stays a small pool rather than a single
      // slot — renderPlantCards just guarantees every filled slot in a
      // given frame shares the same product/class.
      const prodPoolN = config.production.poolHint || 6;
      const dotR = 8, dotGapX = 30;
      const startX = prodX + 26, startY = prodY + prodH - 22;
      const slots = [];
      for (let i = 0; i < prodPoolN; i++) {
        const cx = startX + i * dotGapX;
        if (cx > prodX + prodW - 20) break;
        const cy = startY;
        const c = _ns('circle');
        c.setAttribute('cx', cx); c.setAttribute('cy', cy); c.setAttribute('r', dotR);
        c.setAttribute('fill', 'none'); c.setAttribute('stroke', PLANT_COLORS.slotEmpty);
        c.setAttribute('stroke-width', 1.25); c.setAttribute('stroke-dasharray', '2 2');
        root.appendChild(c);
        slots.push({ x: cx, y: cy });
      }
      positions.productionSlots = slots;
    }

    // ---- Supermarket ---------------------------------------------------------
    root.appendChild(_svgRect(smX, smY, smW, smH, {}));
    root.appendChild(_svgText(smX + 14, smY + 20, 'SUPERMARKET', { size: 11, weight: 700, letterSpacing: '.03em' }));
    root.appendChild(_svgLine(prodX + prodW / 2, prodY + prodH, smX + smW / 2, smY, { marker: true }));
    const restmengeBySachnummer = {};
    restmengeRows.forEach((r) => { restmengeBySachnummer[r.sachnummer] = r; });
    if (hasRestmenge) {
      const dividerX = smX + smW - restmengeColW;
      root.appendChild(_svgLine(dividerX, smY + smHeaderH - 6, dividerX, smY + smH - 8, { stroke: PLANT_COLORS.boxStroke, width: 1.25, dash: '2 3' }));
      root.appendChild(_svgText(dividerX + restmengeColW / 2, smY + 20, 'RESTMENGE', { size: 8, weight: 700, anchor: 'middle', fill: PLANT_COLORS.textFaint, letterSpacing: '.02em' }));
    }
    smRows.forEach((row, ri) => {
      const ry = smY + smHeaderH + ri * smRowH + 8;
      root.appendChild(_svgText(smX + 14, ry + slotSize / 2 + 4, row.label, { size: 10.5, weight: 600, fill: PLANT_COLORS.textMuted }));
      const slotsStartX = smX + smLabelW;
      const rowSlots = [];
      for (let s = 0; s < row.slots; s++) {
        const sx = slotsStartX + s * (slotSize + slotGap);
        root.appendChild(_slotCell(sx, ry, slotSize));
        rowSlots.push({ x: sx + slotSize / 2, y: ry + slotSize / 2, overflow: false });
      }
      if (row.isExotic) {
        const overflowGap = slotGap + 10;
        const overflowStartX = slotsStartX + row.slots * (slotSize + slotGap) + overflowGap;
        root.appendChild(_svgLine(
          overflowStartX - overflowGap / 2, ry - 4,
          overflowStartX - overflowGap / 2, ry + slotSize + 4,
          { stroke: PLANT_COLORS.amber, width: 1.25, dash: '2 3' }
        ));
        for (let s = 0; s < EXOTIC_OVERFLOW_SLOTS; s++) {
          const sx = overflowStartX + s * (slotSize + slotGap);
          root.appendChild(_slotCell(sx, ry, slotSize, { stroke: PLANT_COLORS.amber, fill: PLANT_COLORS.amberLight, dash: '2 2' }));
          rowSlots.push({ x: sx + slotSize / 2, y: ry + slotSize / 2, overflow: true });
        }
        const overflowLabelX = overflowStartX + (EXOTIC_OVERFLOW_SLOTS * (slotSize + slotGap) - slotGap) / 2;
        root.appendChild(_svgText(overflowLabelX, ry - 6, 'overflow', { size: 8, weight: 700, anchor: 'middle', fill: PLANT_COLORS.amber, letterSpacing: '.03em' }));
      }
      // Restmenge anchor — one live-updating badge per Main-runner
      // product, drawn every frame by renderPlantCards (this function
      // only reserves the spot). Several physical rows can share the
      // same sachnummer (see this function's header comment); when they
      // do, each row overwrites the anchor in turn so the LAST physical
      // row of the group ends up holding the badge, keeping exactly one
      // visible per product rather than one per row.
      if (hasRestmenge && !row.isExotic && restmengeBySachnummer[row.label]) {
        positions.restmengeAnchors[row.label] = {
          x: smX + smW - restmengeColW / 2,
          y: ry + slotSize / 2,
          batchSize: restmengeBySachnummer[row.label].batch_size,
        };
      }
      positions.supermarket.push({ label: row.label, slots: rowSlots });
      positions.supermarketByRowNumber[row.row_number != null ? row.row_number : (ri + 1)] = rowSlots;
    });

    // ---- Loop-back: Supermarket -> Collection Box (left margin) ------------
    {
      const loopX = pad / 2 + 2;
      const startX = smX, startY = smY + smH / 2;
      const endX = cbX, endY = cbY + cbH / 2;
      const d = `M ${startX},${startY} L ${loopX},${startY} L ${loopX},${endY} L ${endX},${endY}`;
      root.appendChild(_svgPath(d, { stroke: PLANT_COLORS.blue, width: 1.75, dash: '5 4', marker: 'blue' }));
      root.appendChild(_svgTextRotated(14, (startY + endY) / 2, 'CARD WITHDRAWN', -90, { size: 9, weight: 700, fill: PLANT_COLORS.blue, letterSpacing: '.03em' }));
      positions.transitAnchors.withdrawn = { x: loopX, y: (startY + endY) / 2 };
    }

    return { positions, width: totalW, height: totalH };
  }

  // ===========================================================================
  // Segment normalization
  // ===========================================================================

  // Display-label overrides for raw station names coming back from the
  // backend (movement_trace._STATION_FIELDS). The backend's internal
  // name for the 4th station is "Inspect" — the shop floor calls this
  // station "Sichtprüfung", so relabel it for display everywhere a
  // part's station appears (track box, event log, marker label).
  const PART_STATION_LABELS = { Inspect: 'Sichtpr\u00fcfung' };
  function partStationLabel(name) { return PART_STATION_LABELS[name] || name; }

  // A push/Kanban Part's `stations` array (from movement_trace.
  // build_part_trace_payload) is already {name, enter_t, exit_t} per
  // station, in process order — relabel the keys, translating the raw
  // station name through partStationLabel() so the label used here is
  // consistent with the one used to key renderStationTrack's position
  // map (see PUSH_STATIONS in movement_simulation.html).
  function partToSegments(part) {
    return (part.stations || []).map(st => ({ label: partStationLabel(st.name), enter_t: st.enter_t, exit_t: st.exit_t }));
  }

  // A KanbanCard's `transitions` array [{state,t}] (from movement_trace.
  // build_card_trace_payload) has no explicit end time per state — each
  // transition dwells until the next one starts. The final transition
  // has no "next", so its segment runs to `endT` (pass the requested
  // day window's end_t, already in the same time_unit as the trace).
  function cardToSegments(card, endT) {
    const t = card.transitions || [];
    const segs = [];
    for (let i = 0; i < t.length; i++) {
      const enter_t = t[i].t;
      const exit_t = (i + 1 < t.length) ? t[i + 1].t : (endT != null ? endT : enter_t);
      segs.push({ label: t[i].state, enter_t, exit_t: Math.max(exit_t, enter_t) });
    }
    return segs;
  }

  // Card states come back as raw identifiers (e.g. "in_supermarket").
  // Known ones get a clean label; anything unrecognized is humanized
  // rather than dropped, so a future new state still renders sensibly.
  // Display order requested for the station track (left to right):
  // Batch Collector, Collection Box, Released to Chute, Production,
  // Supermarket. Note this is a DISPLAY order for the track's boxes,
  // not the physical process order (supermarket -> collection box ->
  // batch collector -> chute -> production) — a card's dot still jumps
  // to wherever its actual chronological transitions send it.
  const CARD_STATE_ORDER = ['in_batch_collector', 'released_to_chute', 'in_production', 'in_supermarket', 'withdrawn', 'in_collection_box'];
  const CARD_STATE_LABELS = {
    in_batch_collector: 'Batch Collector',
    released_to_chute: 'Released to Chute',
    in_production: 'Production',
    in_supermarket: 'Supermarket',
    withdrawn: 'Withdrawn',
    in_collection_box: 'Collection Box',
  };
  function cardStateLabel(state) {
    if (CARD_STATE_LABELS[state]) return CARD_STATE_LABELS[state];
    return String(state).replace(/^in_/, '').split('_')
      .map(w => w ? w[0].toUpperCase() + w.slice(1) : w).join(' ');
  }
  // Station-track label order for a given set of segments: canonical
  // states first (in CARD_STATE_ORDER), then any unrecognized ones
  // appended in first-seen order — so the track never silently drops a
  // state the trace actually contains.
  function cardStationOrder(segments) {
    const seen = [];
    segments.forEach(s => { if (!seen.includes(s.label)) seen.push(s.label); });
    const known = CARD_STATE_ORDER.filter(s => seen.includes(s));
    const unknown = seen.filter(s => !CARD_STATE_ORDER.includes(s));
    return known.concat(unknown);
  }

  // ===========================================================================
  // Position interpolation
  // ===========================================================================

  // segments: chronological [{label, enter_t, exit_t}]. positions: label
  // -> {x,y} (station-box centers, keyed the same way as segment labels
  // — for cards this must be the RAW state string, not the display
  // label, since that's what's in the segment; map display labels only
  // in the rendered box text, not the position key).
  function positionAtTime(segments, positions, t) {
    if (!segments.length) return null;
    if (t <= segments[0].enter_t) {
      const p = positions[segments[0].label];
      return p ? { x: p.x, y: p.y, label: segments[0].label, phase: 'at' } : null;
    }
    for (let i = 0; i < segments.length; i++) {
      const seg = segments[i];
      if (t >= seg.enter_t && t <= seg.exit_t) {
        const p = positions[seg.label];
        return p ? { x: p.x, y: p.y, label: seg.label, phase: 'at' } : null;
      }
      const next = segments[i + 1];
      if (next && t > seg.exit_t && t < next.enter_t) {
        const p0 = positions[seg.label], p1 = positions[next.label];
        if (!p0 || !p1) return null;
        const frac = (t - seg.exit_t) / Math.max(next.enter_t - seg.exit_t, 1e-6);
        return {
          x: p0.x + (p1.x - p0.x) * frac,
          y: p0.y + (p1.y - p0.y) * frac,
          label: seg.label + ' \u2192 ' + next.label,
          phase: 'transit',
        };
      }
    }
    const last = segments[segments.length - 1];
    const p = positions[last.label];
    return p ? { x: p.x, y: p.y, label: last.label, phase: 'at' } : null;
  }

  // ===========================================================================
  // Playback controller — owns the clock only; caller renders on each tick.
  // ===========================================================================
  function createPlayer(els, opts) {
    const { sliderEl, playBtnEl, speedEl, timeLabelEl, timeInputEl } = els;
    const tMin = opts.tMin, tMax = opts.tMax, unit = opts.unit || 'h';
    const onTick = opts.onTick || function () {};
    const playSeconds = opts.playSeconds || 20; // wall-clock seconds for a full 1x playthrough
    let playing = false;
    let raf = null;
    let lastWall = null;
    let t = tMin;

    sliderEl.min = tMin;
    sliderEl.max = tMax;
    sliderEl.step = (tMax - tMin) / 1000 || 0.001;
    sliderEl.value = tMin;

    // Direct time entry — a text field the caller formats/parses however
    // makes sense for its own domain (e.g. "Day 2, 12:52:40" for a
    // continuous, day-stitched sim clock — see parseDateTimeInput), NOT
    // assumed to be a raw number in the slider's own unit. Falls back to
    // a plain float in the slider's unit if the caller doesn't supply
    // timeInputFormatter/timeInputParser. Kept in sync with playback/
    // scrubbing below, except while the user is actively focused on it
    // (so typing isn't clobbered mid-edit).
    const fmtInput = opts.timeInputFormatter || opts.timeFormatter || ((tv) => `${tv.toFixed(3)} ${unitLabel(unit)}`);
    const parseInput = opts.timeInputParser || ((str) => { const v = parseFloat(str); return isNaN(v) ? null : v; });
    if (timeInputEl) {
      timeInputEl.value = fmtInput(tMin);
    }

    function render() {
      sliderEl.value = t;
      if (timeLabelEl) {
        timeLabelEl.textContent = opts.timeFormatter
          ? opts.timeFormatter(t)
          : `${t.toFixed(3)} ${unitLabel(unit)}`;
      }
      if (timeInputEl && document.activeElement !== timeInputEl) {
        timeInputEl.value = fmtInput(t);
      }
      onTick(t);
    }

    function step(wallNow) {
      if (!playing) return;
      if (lastWall == null) lastWall = wallNow;
      const dtWall = (wallNow - lastWall) / 1000;
      lastWall = wallNow;
      const speed = parseFloat(speedEl.value) || 1;
      const simPerWallSecond = ((tMax - tMin) / playSeconds) * speed;
      t += dtWall * simPerWallSecond;
      if (t >= tMax) {
        t = tMax;
        playing = false;
        playBtnEl.textContent = '\u25B6 Play';
        render();
        return;
      }
      render();
      raf = requestAnimationFrame(step);
    }

    playBtnEl.addEventListener('click', () => {
      playing = !playing;
      playBtnEl.textContent = playing ? '\u23F8 Pause' : '\u25B6 Play';
      if (playing) {
        if (t >= tMax) t = tMin;
        lastWall = null;
        raf = requestAnimationFrame(step);
      } else if (raf) {
        cancelAnimationFrame(raf);
      }
    });
    sliderEl.addEventListener('input', () => {
      playing = false;
      playBtnEl.textContent = '\u25B6 Play';
      if (raf) cancelAnimationFrame(raf);
      t = parseFloat(sliderEl.value);
      render();
    });

    if (timeInputEl) {
      const jumpToInput = () => {
        const v = parseInput(timeInputEl.value);
        if (v == null || isNaN(v)) { timeInputEl.value = fmtInput(t); return; }
        playing = false;
        playBtnEl.textContent = '\u25B6 Play';
        if (raf) cancelAnimationFrame(raf);
        t = Math.min(Math.max(v, tMin), tMax);
        render();
      };
      timeInputEl.addEventListener('change', jumpToInput);
      timeInputEl.addEventListener('keydown', (e) => {
        if (e.key === 'Enter') { e.preventDefault(); jumpToInput(); timeInputEl.blur(); }
      });
    }

    render();
    return {
      setTime(newT) { t = newT; render(); },
      stop() { playing = false; if (raf) cancelAnimationFrame(raf); },
    };
  }

  // ===========================================================================
  // Event log — a per-station (piece) or per-transition (card) list of
  // timestamps below the track, each with a small dot that fills in as
  // playback passes it (empty/faded = not reached yet, filled = passed).
  // ===========================================================================

  // rows: [{ label, marks: [{ dotLabel, t, timeText }] }]
  // one row per station (piece, 2 marks: start/end) or per transition
  // (card, 1 mark). Rebuilt whenever a new entity is selected.
  function renderEventLog(containerEl, rows) {
    containerEl.innerHTML = '';
    rows.forEach((row) => {
      const rowEl = document.createElement('div');
      rowEl.className = 'event-row';

      const labelEl = document.createElement('div');
      labelEl.className = 'event-row-label';
      labelEl.textContent = row.label;
      rowEl.appendChild(labelEl);

      const marksEl = document.createElement('div');
      marksEl.className = 'event-row-marks';
      row.marks.forEach((mk) => {
        const markEl = document.createElement('span');
        markEl.className = 'event-mark';
        markEl.dataset.t = mk.t;

        const dotEl = document.createElement('span');
        dotEl.className = 'event-dot';
        markEl.appendChild(dotEl);

        const txtEl = document.createElement('span');
        txtEl.className = 'event-mark-text';
        txtEl.textContent = mk.dotLabel ? `${mk.dotLabel} ${mk.timeText}` : mk.timeText;
        markEl.appendChild(txtEl);

        marksEl.appendChild(markEl);
      });
      rowEl.appendChild(marksEl);

      containerEl.appendChild(rowEl);
    });
  }

  // Called on every playback tick — toggles each mark between
  // faded/empty (not reached) and filled (passed), no DOM rebuild.
  function updateEventLogDots(containerEl, currentT) {
    const marks = containerEl.querySelectorAll('.event-mark');
    for (let i = 0; i < marks.length; i++) {
      const el = marks[i];
      const t = parseFloat(el.dataset.t);
      el.classList.toggle('passed', currentT >= t);
    }
  }

  // ===========================================================================
  // Step 3 — animating real cards into the Step 1 layout.
  //
  // Consumes GET /api/mixed/plant_structure (static shape -> config for
  // renderPlantLayout, via buildPlantConfigFromStructure) and GET
  // /api/mixed/movement_state (per-frame per-line card occupancy, via
  // renderPlantCards) — see api_server_mixed.py's _build_plant_structure_
  // payload / _build_movement_state_payload docstrings for exactly what
  // each field means and which approximations it makes. This module only
  // draws what those payloads report; it has no opinion on which line/day/
  // window to request (that's movement_simulation.html's job).
  // ===========================================================================

  // Deterministic color per product_type, independent of iteration order —
  // sorted alphabetically before assigning palette indices so the same
  // product gets the same color on every tick / page load.
  function productColorMap(productTypes) {
    const sorted = Array.from(new Set((productTypes || []).filter(Boolean))).sort();
    const map = {};
    sorted.forEach((p, i) => { map[p] = generateColor(i); });
    return map;
  }

  function _ns2(tag) { return document.createElementNS('http://www.w3.org/2000/svg', tag); }

  // Best-effort card_id -> product_type cache, built up as tokens are
  // drawn in Supermarket/Batch Collector/Chute/Production (all of which
  // key a card's color off a *row's* product, or the entry's own
  // product_type — never a bare card_id with no product attached).
  // Collection Box entries, as actually returned by movement_state, are
  // sometimes bare card_ids (or objects without product_type) rather
  // than {card_id, product_type} — this fills that gap in from
  // whichever OTHER structure the same card_id was last seen in, so its
  // token still matches the color it has everywhere else in the
  // diagram. Cleared via resetCardProductCache() whenever a different
  // line/window is loaded, so a ded id can't leak a stale product
  // across runs.
  let _cardProductCache = {};
  function _rememberCardProduct(cardId, product) {
    if (cardId != null && product) _cardProductCache[cardId] = product;
  }
  function _lookupCardProduct(cardId) {
    return cardId != null ? (_cardProductCache[cardId] || null) : null;
  }
  function resetCardProductCache() { _cardProductCache = {}; }

  // Scans every frame's Supermarket/Batch Collector/Chute/Production
  // entries up front (each of which carries a real product, unlike some
  // Collection Box entries — see _cardProductCache's docstring above)
  // and seeds the cache with all of them, so even the very first frame
  // rendered already has correct Collection Box colors instead of only
  // catching up as playback scrubs past other structures.
  function primeCardProductCache(frames) {
    (frames || []).forEach((f) => (f.lines || []).forEach((lf) => {
      (lf.supermarket_rows || []).forEach((row) => {
        if (row.is_exotic) return;
        (row.card_ids || []).forEach((cid) => _rememberCardProduct(cid, row.sachnummer));
      });
      (lf.batch_collector_rows || []).forEach((row) => {
        (row.card_ids || []).forEach((cid) => _rememberCardProduct(cid, row.product_type));
        (row.overflow_card_ids || []).forEach((cid) => _rememberCardProduct(cid, row.product_type));
      });
      const chuteEntries = (lf.chute || {}).entries || [];
      chuteEntries.forEach((entry) => _rememberCardProduct(entry.id, entry.product_type));
      (lf.in_transit || []).forEach((c) => _rememberCardProduct(c.card_id, c.product_type));
    }));
  }

  // One small colored card token at (x,y) — filled circle + card id
  // (when it fits) + a <title> tooltip. `frozen` draws an amber ring
  // (Chute frozen zone). `rush` draws a red ring plus a small "RUSH"
  // tag above the token, taking visual priority over `frozen` (the two
  // are mutually exclusive in practice — a rush entry can only ever
  // land in the movable tail, never inside the locked frozen zone — but
  // rush wins the styling if both were ever set).
  function _cardToken(x, y, r, cardId, color, opts) {
    opts = opts || {};
    const g = _ns2('g');
    const c = _ns2('circle');
    c.setAttribute('cx', x); c.setAttribute('cy', y); c.setAttribute('r', r);
    c.setAttribute('fill', color);
    const ringColor = opts.rush ? PLANT_COLORS.red : (opts.frozen || opts.overflow) ? PLANT_COLORS.amber : '#fff';
    c.setAttribute('stroke', ringColor);
    c.setAttribute('stroke-width', (opts.rush || opts.frozen || opts.overflow) ? 2.25 : 1.25);
    if (opts.overflow) c.setAttribute('stroke-dasharray', '2 2');
    g.appendChild(c);
    if (opts.showLabel !== false && r >= 8) {
      const t = _ns2('text');
      t.setAttribute('x', x); t.setAttribute('y', y + 3.2);
      t.setAttribute('text-anchor', 'middle');
      t.setAttribute('font-size', Math.min(9, r + 1));
      t.setAttribute('font-weight', 700);
      t.setAttribute('fill', '#fff');
      t.textContent = String(cardId);
      g.appendChild(t);
    }
    if (opts.rush) {
      const tagW = 26, tagH = 11;
      const tagX = x - tagW / 2, tagY = y - r - tagH - 2;
      const bg = _ns2('rect');
      bg.setAttribute('x', tagX); bg.setAttribute('y', tagY);
      bg.setAttribute('width', tagW); bg.setAttribute('height', tagH);
      bg.setAttribute('rx', 3);
      bg.setAttribute('fill', PLANT_COLORS.red);
      g.appendChild(bg);
      const tag = _ns2('text');
      tag.setAttribute('x', x); tag.setAttribute('y', tagY + tagH - 2.7);
      tag.setAttribute('text-anchor', 'middle');
      tag.setAttribute('font-size', 7.5);
      tag.setAttribute('font-weight', 700);
      tag.setAttribute('letter-spacing', '.03em');
      tag.setAttribute('fill', '#fff');
      tag.textContent = 'RUSH';
      g.appendChild(tag);
    }
    if (opts.title) {
      const title = _ns2('title');
      title.textContent = opts.title;
      g.appendChild(title);
    }
    return g;
  }

  // A small "+N" count badge — used for overflow (past a box's drawn
  // capacity) and for in-transit groups that don't get one token per card.
  function _countBadge(x, y, n, opts) {
    opts = opts || {};
    const g = _ns2('g');
    const r = 11;
    const c = _ns2('circle');
    c.setAttribute('cx', x); c.setAttribute('cy', y); c.setAttribute('r', r);
    c.setAttribute('fill', opts.fill || PLANT_COLORS.textMuted);
    c.setAttribute('stroke', '#fff'); c.setAttribute('stroke-width', 1.5);
    g.appendChild(c);
    const t = _ns2('text');
    t.setAttribute('x', x); t.setAttribute('y', y + 3.2);
    t.setAttribute('text-anchor', 'middle');
    t.setAttribute('font-size', 9.5); t.setAttribute('font-weight', 700);
    t.setAttribute('fill', '#fff');
    t.textContent = `+${n}`;
    g.appendChild(t);
    if (opts.title) { const ti = _ns2('title'); ti.textContent = opts.title; g.appendChild(ti); }
    return g;
  }

  // Draws (or redraws) the card layer on top of a renderPlantLayout()
  // result. `layout` is renderPlantLayout()'s return value; `lineFrame`
  // is one entry of a movement_state response's frames[i].lines[] array
  // ({supermarket_rows, restmenge, batch_collector_rows, collection_box,
  // chute, production_status, production_occupants, in_transit} — see
  // _build_movement_state_payload's docstring). `colorMap` is a
  // product_type -> color dict (see productColorMap). Safe to call every
  // playback tick: it clears and rebuilds its own <g> layer rather than
  // diffing, since one frame is small (tens of cards).
  //
  // When the frame was fetched with include_push=true (the "All
  // Production" tab), Exotic supermarket rows carry `is_exotic` +
  // either the legacy single-product `sachnummer`/`push_count` pair or a
  // `products: [{sachnummer, push_count}, ...]` list (push pieces have no
  // id — see _build_movement_state_payload's docstring) and are drawn as
  // plain filled slots rather than individually-labeled tokens. A row
  // that hasn't fully emptied before a new product starts filling it
  // legitimately holds more than one product at once — `products` lets
  // each occupy its own contiguous block of slots (own color + label),
  // filled in list order; the legacy single-field shape is still accepted
  // and treated as a one-entry `products` list. Any count past the row's
  // drawn capacity (including the EXOTIC_OVERFLOW_SLOTS drawn past normal
  // capacity) collapses into a single "+N" badge. `restmenge` (loose,
  // not-yet-a-whole-card pieces per product) is drawn as a small live
  // number at the anchor renderPlantLayout reserved per sachnummer
  // (positions.restmengeAnchors) — separate from, and drawn alongside,
  // whatever's currently in the Production rectangle, since a card can
  // legitimately still be "in production" while pieces it already
  // contributed are already counted here. The Production rectangle
  // itself is now driven by `production_status`/`production_occupants`,
  // NOT `in_transit` — those two fields are the single source of truth
  // for "what's really on the line right now" (including push chunks,
  // which never get a KanbanCard and so never appeared in `in_transit`
  // before); `in_transit` only carries "withdrawn" going forward. `chute.
  // entries` (see below) merges pull cards and push chunks into one
  // queue, each entry colored by its own product_type the same way
  // Supermarket/Batch Collector tokens are — replaces the old pull-only
  // card_ids + anonymous push_pending badge.
  function renderPlantCards(svgEl, layout, lineFrame, colorMap) {
    // Appended inside the same scale-transform group renderPlantLayout
    // drew into (see _startScaledSvg/_scaleRootOf), using the same
    // unscaled coordinates as `layout.positions` — otherwise these card
    // tokens would render at 1:1 scale while the slots they sit in are
    // shrunk by DIAGRAM_SCALE, and everything would drift out of place.
    const scaleRoot = _scaleRootOf(svgEl);
    let g = scaleRoot.querySelector('#plantCardsLayer');
    if (g) g.remove();
    g = _ns2('g');
    g.setAttribute('id', 'plantCardsLayer');
    scaleRoot.appendChild(g);
    if (!lineFrame) return;
    const positions = layout.positions;
    const slotSize = 20; // must match renderPlantLayout's slotSize
    const r = slotSize / 2 - 2;
    const colorFor = (p) => (p && colorMap[p]) || PLANT_COLORS.blue;

    // ---- Supermarket rows (Main-runner, card-level; Exotic, push count-only) --
    (lineFrame.supermarket_rows || []).forEach((row) => {
      const slots = positions.supermarketByRowNumber[row.row_number];
      if (!slots) return; // overflow entry (row_number null) — no physical slot to draw into
      if (row.is_exotic) {
        // Push-produced pieces have no card_id — draw an occupancy count
        // instead of per-piece tokens, filling left-to-right so the row
        // still visibly "fills" and "empties". A row that hasn't fully
        // emptied before a different product starts filling it holds more
        // than one product at once, so accept either the multi-product
        // `row.products` list or fall back to the legacy single-product
        // `sachnummer`/`push_count` pair as a one-entry list.
        const products = (Array.isArray(row.products) && row.products.length)
          ? row.products
          : (row.sachnummer != null || row.push_count)
            ? [{ sachnummer: row.sachnummer, push_count: row.push_count || 0 }]
            : [];

        let offset = 0;
        let totalCount = 0;
        products.forEach((prod) => {
          const count = prod.push_count || 0;
          totalCount += count;
          const color = colorFor(prod.sachnummer);
          const n = Math.max(0, Math.min(count, slots.length - offset));
          let lastSlot = null;
          for (let i = 0; i < n; i++) {
            const slot = slots[offset + i];
            lastSlot = slot;
            g.appendChild(_cardToken(slot.x, slot.y, r, '', color, {
              showLabel: false,
              overflow: slot.overflow,
              title: `${prod.sachnummer || 'Exotic'} \u2014 Supermarket row ${row.row_number} (push, ${count} pc${slot.overflow ? ', in overflow slots' : ''}, no individual piece id)`,
            }));
          }
          if (lastSlot && prod.sachnummer) {
            const t = _ns2('text');
            t.setAttribute('x', lastSlot.x + slotSize * 0.75);
            t.setAttribute('y', lastSlot.y + 3.2);
            t.setAttribute('font-size', 9.5);
            t.setAttribute('font-weight', 700);
            t.setAttribute('fill', color);
            t.textContent = prod.sachnummer;
            const title = _ns2('title');
            title.textContent = `${prod.sachnummer} \u2014 Supermarket row ${row.row_number} (push, ${count} pc)`;
            t.appendChild(title);
            g.appendChild(t);
          }
          offset += count;
        });
        if (totalCount > slots.length) {
          const last = slots[slots.length - 1];
          g.appendChild(_countBadge(last.x + slotSize, last.y, totalCount - slots.length, {
            fill: PLANT_COLORS.amber,
            title: `${totalCount} pc total in this Exotic row, past drawn capacity (incl. ${EXOTIC_OVERFLOW_SLOTS} drawn overflow slots)`,
          }));
        }
        return;
      }
      (row.card_ids || []).forEach((cid, i) => {
        const slot = slots[i];
        _rememberCardProduct(cid, row.sachnummer);
        if (!slot) return;
        g.appendChild(_cardToken(slot.x, slot.y, r, cid, colorFor(row.sachnummer), {
          title: `Card #${cid} \u2014 ${row.sachnummer} \u2014 Supermarket row ${row.row_number}`,
        }));
      });
    });

    // ---- Restmenge: loose pieces per product, redrawn every frame ------
    // (_build_movement_state_payload's "restmenge" field — pcs_partial,
    // replayed at t_s). Drawn at the anchor renderPlantLayout reserved
    // per sachnummer (positions.restmengeAnchors) — a product with no
    // reserved anchor (e.g. Exotic-only / not stocked as a Main-runner
    // row on this line) is silently skipped rather than mis-drawn.
    (lineFrame.restmenge || []).forEach((rm) => {
      const anchor = positions.restmengeAnchors && positions.restmengeAnchors[rm.sachnummer];
      if (!anchor) return;
      const pcs = rm.pcs_partial || 0;
      const t = _ns2('text');
      t.setAttribute('x', anchor.x);
      t.setAttribute('y', anchor.y + 3.2);
      t.setAttribute('text-anchor', 'middle');
      t.setAttribute('font-size', 10);
      t.setAttribute('font-weight', 700);
      t.setAttribute('fill', pcs > 0 ? PLANT_COLORS.blue : PLANT_COLORS.textFaint);
      t.textContent = `+${pcs}`;
      const batchSize = rm.batch_size || anchor.batchSize;
      const title = _ns2('title');
      title.textContent = `${rm.sachnummer} \u2014 Restmenge: ${pcs}${batchSize ? ` / ${batchSize}` : ''} pcs toward the next full card`;
      t.appendChild(title);
      g.appendChild(t);
    });

    // ---- Batch collector rows ------------------------------------------
    (lineFrame.batch_collector_rows || []).forEach((row) => {
      const slots = positions.batchCollectorByProduct[row.product_type];
      if (!slots) return;
      (row.card_ids || []).forEach((cid, i) => {
        const slot = slots[i];
        _rememberCardProduct(cid, row.product_type);
        if (!slot) return;
        g.appendChild(_cardToken(slot.x, slot.y, r, cid, colorFor(row.product_type), {
          title: `Card #${cid} \u2014 ${row.product_type} \u2014 Batch Collector (trigger ${row.trigger_amount})`,
        }));
      });
      (row.overflow_card_ids || []).forEach((cid) => _rememberCardProduct(cid, row.product_type));
      if (row.overflow_card_ids && row.overflow_card_ids.length && slots.length) {
        const last = slots[slots.length - 1];
        g.appendChild(_countBadge(last.x + slotSize, last.y, row.overflow_card_ids.length, {
          fill: PLANT_COLORS.amber,
          title: `${row.overflow_card_ids.length} card(s) past drawn capacity: ${row.overflow_card_ids.join(', ')}`,
        }));
      }
    });

    // ---- Collection box (cycles through the drawn pool if oversubscribed) --
    // One physical box shared by every product on the line, so each
    // token is still colored by its own product_type (now supplied by
    // the backend per-card) — same idea as Supermarket/Batch Collector,
    // just without dedicated per-product slots to sort into.
    {
      const cbCards = (lineFrame.collection_box || {}).card_ids || [];
      const slots = positions.collectionBoxSlots;
      if (slots && slots.length) {
        cbCards.forEach((c, i) => {
          const slot = slots[i % slots.length];
          const cid = (c && typeof c === 'object') ? c.card_id : c;
          const product = ((c && typeof c === 'object') ? c.product_type : null) || _lookupCardProduct(cid);
          g.appendChild(_cardToken(slot.x, slot.y, 6.5, cid, colorFor(product), {
            showLabel: false,
            title: `Card #${cid}${product ? ' \u2014 ' + product : ''} \u2014 Collection Box`,
          }));
        });
        if (cbCards.length > slots.length) {
          const last = slots[slots.length - 1];
          g.appendChild(_countBadge(last.x, last.y - 18, cbCards.length, {
            title: `${cbCards.length} card(s) total in Collection Box (drawn pool is illustrative, not a real capacity)`,
          }));
        }

        // ---- Per-product color + count legend --------------------------
        // The dot pool above only shows a total (and only the drawn/
        // cycling subset, once oversubscribed) — this breaks that total
        // down by product (every card actually in the box, not just the
        // drawn subset), each line colored to match its token color, so
        // it reads the same way as the Supermarket/Batch Collector rows.
        const box = positions.collectionBoxBox;
        if (box) {
          const counts = {};
          cbCards.forEach((c) => {
            const cid = (c && typeof c === 'object') ? c.card_id : c;
            const product = ((c && typeof c === 'object') ? c.product_type : null) || _lookupCardProduct(cid);
            const key = product || 'Unknown';
            counts[key] = (counts[key] || 0) + 1;
          });
          const productList = Object.keys(counts).sort();
          const maxLines = 3;
          const shown = productList.slice(0, maxLines);
          const legendX = box.x + 14;
          // Fixed start just below the dot pool (poolHint defaults to 8 =
          // 2 rows), leaving room below for up to maxLines + 1 ("+N more").
          let legendY = box.y + 100;
          shown.forEach((p) => {
            const sw = _ns2('rect');
            sw.setAttribute('x', legendX); sw.setAttribute('y', legendY - 7);
            sw.setAttribute('width', 8); sw.setAttribute('height', 8);
            sw.setAttribute('rx', 2);
            sw.setAttribute('fill', p === 'Unknown' ? PLANT_COLORS.slotEmpty : colorFor(p));
            g.appendChild(sw);
            let label = p;
            if (label.length > 15) label = label.slice(0, 14) + '\u2026';
            const t = _svgText(legendX + 13, legendY, `${label} \u00d7${counts[p]}`, { size: 9, weight: 600, fill: PLANT_COLORS.textMuted });
            const title = _ns2('title');
            title.textContent = `${p}: ${counts[p]} card(s) in Collection Box`;
            t.appendChild(title);
            g.appendChild(t);
            legendY += 13;
          });
          if (productList.length > maxLines) {
            g.appendChild(_svgText(legendX, legendY, `+${productList.length - maxLines} more product(s)`, { size: 9, fill: PLANT_COLORS.textFaint }));
          }
        }
      }
    }

    // ---- Chute — merged pull cards + push chunks, oldest-first, filled
    // from the production end upward (mirrors the backend's "index 0 =
    // closest to production" convention). Each entry is colored by its
    // own product_type, same as Supermarket/Batch Collector/Collection
    // Box, so it's easy to see at a glance which products are queued.
    // Push entries (kind:'push') have no real per-piece id — drawn the
    // same as pull cards but without a label, and their tooltip says so.
    {
      const chuteEntries = (lineFrame.chute || {}).entries
        // Back-compat: older payloads only had card_ids (pull-only).
        || ((lineFrame.chute || {}).card_ids || []).map((cid) => ({ kind: 'pull', id: cid, product_type: null, frozen: false, rush: false }));
      const slots = positions.chute || [];
      const cap = slots.length;
      chuteEntries.forEach((entry, i) => {
        const product = entry.product_type || _lookupCardProduct(entry.id);
        _rememberCardProduct(entry.id, entry.product_type);
        const slotIdx = cap - 1 - i;
        if (slotIdx < 0) return; // past drawn capacity — see chute.overflow_entries
        const slot = slots[slotIdx];
        const isPush = entry.kind === 'push';
        const rushSuffix = entry.rush ? ' \u2014 RUSH' : '';
        g.appendChild(_cardToken(slot.x, slot.y, r, isPush ? '' : entry.id, colorFor(product), {
          showLabel: !isPush,
          frozen: entry.frozen,
          rush: !!entry.rush,
          title: isPush
            ? `Push chunk \u2014 ${product || 'unknown product'} \u2014 Chute${entry.frozen ? ' (frozen zone)' : rushSuffix} (no individual piece id)`
            : `Card #${entry.id}${product ? ' \u2014 ' + product : ''} \u2014 Chute${entry.frozen ? ' (frozen zone)' : rushSuffix}`,
        }));
      });
      const overflowEntries = (lineFrame.chute || {}).overflow_entries || (lineFrame.chute || {}).overflow_card_ids;
      if (overflowEntries && overflowEntries.length && slots.length) {
        const top = slots[0];
        g.appendChild(_countBadge(top.x, top.y - 16, overflowEntries.length, {
          fill: PLANT_COLORS.amber,
          title: `${overflowEntries.length} entr${overflowEntries.length === 1 ? 'y' : 'ies'} past drawn chute capacity`,
        }));
      }
    }

    // ---- Production rectangle: single current occupant + status label ----
    // Driven by production_status / production_occupants (resolved
    // server-side from kenv.gate_activity_log — see
    // _build_movement_state_payload's docstring), NOT in_transit — the
    // backend no longer sends an 'in_production' entry in in_transit at
    // all, precisely so this is the one place that draws it.
    {
      const status = lineFrame.production_status;
      const occupants = lineFrame.production_occupants || [];
      const box = positions.productionBox;
      if (status && box) {
        const labelX = box.x + box.w - 14;
        const labelY = box.y + 20;
        if (status.state === 'producing') {
          const color = colorFor(status.sachnummer);
          const dot = _ns2('circle');
          dot.setAttribute('cx', labelX - 74); dot.setAttribute('cy', labelY - 4); dot.setAttribute('r', 5);
          dot.setAttribute('fill', color);
          g.appendChild(dot);
          const t = _ns2('text');
          t.setAttribute('x', labelX); t.setAttribute('y', labelY);
          t.setAttribute('text-anchor', 'end');
          t.setAttribute('font-size', 10.5); t.setAttribute('font-weight', 700);
          t.setAttribute('fill', PLANT_COLORS.text);
          t.textContent = `${status.sachnummer}${status.sim_class === 'push' ? ' (push)' : ''}${status.possible_changeover ? ' \u2014 incl. changeover' : ''}`;
          g.appendChild(t);
        } else if (status.state === 'off_shift') {
          // v6: line isn't scheduled to run at all right now, per the
          // workbook's Shifts sheet — distinct from a genuine idle gap on
          // an on-shift line. Uses the same amber tone as the frozen-zone/
          // scheduling cues elsewhere on this rectangle so it visually
          // reads as "not scheduled" rather than plain "nothing running".
          const t = _ns2('text');
          t.setAttribute('x', labelX); t.setAttribute('y', labelY);
          t.setAttribute('text-anchor', 'end');
          t.setAttribute('font-size', 10.5); t.setAttribute('font-weight', 700);
          t.setAttribute('fill', PLANT_COLORS.amber);
          t.setAttribute('letter-spacing', '.03em');
          t.textContent = 'OFF \u2014 not scheduled';
          g.appendChild(t);
        } else {
          const t = _ns2('text');
          t.setAttribute('x', labelX); t.setAttribute('y', labelY);
          t.setAttribute('text-anchor', 'end');
          t.setAttribute('font-size', 10.5); t.setAttribute('font-weight', 700);
          t.setAttribute('fill', PLANT_COLORS.textFaint);
          t.setAttribute('letter-spacing', '.03em');
          t.textContent = 'IDLE \u2014 no active job';
          g.appendChild(t);
        }
      }

      if (occupants.length) {
        const slots = positions.productionSlots || [];
        occupants.forEach((c) => { if (c.kind !== 'push') _rememberCardProduct(c.card_id, c.product_type); });
        if (slots.length) {
          occupants.forEach((c, i) => {
            const slot = slots[i % slots.length];
            g.appendChild(_cardToken(slot.x, slot.y, 8, c.kind === 'push' ? '' : c.card_id, colorFor(c.product_type), {
              showLabel: c.kind !== 'push',
              title: c.kind === 'push'
                ? `Push chunk \u2014 ${c.product_type || 'unknown product'} \u2014 in Production (no individual piece id)`
                : `Card #${c.card_id} \u2014 ${c.product_type || ''} \u2014 in Production`,
            }));
          });
          if (occupants.length > slots.length) {
            const last = slots[slots.length - 1];
            g.appendChild(_countBadge(last.x + 18, last.y - 18, occupants.length - slots.length, {
              fill: PLANT_COLORS.blue,
              title: `${occupants.length} card(s)/chunk(s) total in Production`,
            }));
          }
        } else if (positions.production) {
          // Fallback if a caller-supplied layout config drew no production slots.
          g.appendChild(_countBadge(positions.production.x, positions.production.y, occupants.length, {
            fill: PLANT_COLORS.blue,
            title: `${occupants.length} card(s)/chunk(s) in Production`,
          }));
        }
      }
    }

    // ---- In-transit: withdrawn (the only state left with no Step-1 box) --
    {
      const byState = {};
      (lineFrame.in_transit || []).forEach((c) => {
        (byState[c.state] = byState[c.state] || []).push(c);
      });

      Object.keys(byState).forEach((state) => {
        const anchor = positions.transitAnchors[state];
        if (!anchor) return;
        const list = byState[state];
        g.appendChild(_countBadge(anchor.x, anchor.y, list.length, {
          fill: PLANT_COLORS.blue,
          title: `${list.length} card(s) ${state.replace(/^in_/, '').replace('_', ' ')}: ${list.map(c => '#' + c.card_id).join(', ')}`,
        }));
      });
    }
  }

  // Transforms one line entry of a GET /api/mixed/plant_structure response
  // ({line_id, line_name, supermarket:{rows}, batch_collector:{rows},
  // chute:{frozen_zone_cards, capacity}}) into the config shape
  // renderPlantLayout() expects.
  //
  // opts.includeExotic (default false): Exotic supermarket rows hold
  // push-produced pieces, which never get a KanbanCard (see
  // _build_movement_state_payload's docstring) — the Kanban-only Push/
  // Pull tabs' Plant Layout excludes them (drawing an always-empty row
  // there would misrepresent a Kanban-only card movement view), but the
  // "All Production" tab wants them, since movement_state's
  // include_push=true now reports real push occupancy for them
  // (row.push_count, via kenv.exotic_snapshot_log).
  function buildPlantConfigFromStructure(lineStruct, opts) {
    opts = opts || {};
    const smRows = (lineStruct.supermarket.rows || []).filter((r) => opts.includeExotic || !r.is_exotic);
    const bcRows = lineStruct.batch_collector.rows || [];
    return {
      lineLabel: lineStruct.line_name || `Line ${lineStruct.line_id}`,
      collectionBox: { poolHint: 8 },
      batchCollector: {
        rows: bcRows.map((r) => ({ product: r.product_type, capacity: r.capacity, triggerAmount: r.trigger_amount })),
      },
      chute: { capacity: lineStruct.chute.capacity, frozenZone: lineStruct.chute.frozen_zone_cards },
      supermarket: {
        rows: smRows.map((r) => ({
          label: r.is_exotic ? 'Exotic' : r.label, slots: r.capacity, row_number: r.row_number,
          isExotic: !!r.is_exotic,
        })),
      },
      // Restmenge: straight pass-through of plant_structure's static
      // {sachnummer, batch_size} rows — renderPlantLayout() reserves the
      // badge anchor per product; the live pcs_partial number itself
      // comes from movement_state's per-frame "restmenge" field, filled
      // in by renderPlantCards.
      restmenge: lineStruct.restmenge || [],
    };
  }

  global.MovementLayout = {
    COLOR_PALETTE, generateColor, escapeHtml, unitLabel, formatClock, formatDateTime, parseDateTimeInput, trackEndPadding,
    renderStationTrack,
    partToSegments, partStationLabel, cardToSegments, cardStateLabel, cardStationOrder,
    positionAtTime, createPlayer,
    renderEventLog, updateEventLogDots,
    renderPlantLayout, PLANT_COLORS, DEFAULT_PLANT_CONFIG,
    productColorMap, renderPlantCards, buildPlantConfigFromStructure,
    resetCardProductCache, primeCardProductCache,
  };
})(window);
