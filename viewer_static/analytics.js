// Analytics tab: corpus-level scale/density stats over the same segment set
// classifier.py's build_corpus() actually trains on (see corpus_analytics.py), plus a
// metadata breakdown (bar/line/scatter depending on field type) and, for a categorical
// breakdown, a vocabulary-overlap panel.
//
// Reuses `escapeHtml` from app.js. Charts are hand-rolled SVG strings (no charting
// library -- see CLAUDE.md's no-build-step rule and the design brief's offline
// requirement) built the same way the rest of this app builds HTML: template literals
// assigned to innerHTML.

const analyticsState = {
  fields: [],
  selectedField: null,
  normalize: false,
  lastBreakdown: null, // the last /breakdown response, cached so the normalize toggle
                        // can re-render without a round-trip
};

// --- SVG chart primitives ---------------------------------------------------------------

const CHART_W = 640;
const CHART_H = 260;
const CHART_PAD_L = 54;
const CHART_PAD_R = 16;
const CHART_PAD_T = 16;
const CHART_PAD_B = 64;

function svgOpen(w, h) {
  return `<svg viewBox="0 0 ${w} ${h}" width="100%" style="max-width:${w}px" role="img">`;
}

function truncateLabel(s, n) {
  s = String(s);
  return s.length > n ? s.slice(0, n - 1) + "…" : s;
}

// "Unknown" (null-valued documents) and "Other" (the overflow bucket beyond
// MAX_CATEGORICAL_GROUPS -- see corpus_analytics.py) are synthetic labels, not real field
// values -- clicking them can't be turned into a meaningful summary?field=&value= filter,
// so they never get the clickable affordance even when a breakdown is otherwise drillable.
function isSyntheticGroupLabel(value) {
  return value === "Unknown" || value === "Other";
}

function renderBarChart(container, groups, { normalize = false, onBarClick = null } = {}) {
  if (!groups.length) {
    container.innerHTML = '<div class="hint">No data for this breakdown.</div>';
    return;
  }
  const values = groups.map((g) =>
    normalize ? (g.document_count ? g.word_count / g.document_count : 0) : g.word_count
  );
  const maxV = Math.max(...values, 1);
  const plotW = CHART_W - CHART_PAD_L - CHART_PAD_R;
  const plotH = CHART_H - CHART_PAD_T - CHART_PAD_B;
  const slot = plotW / groups.length;
  const barW = Math.max(6, slot - 10);

  let bars = "";
  let labels = "";
  groups.forEach((g, i) => {
    const v = values[i];
    const h = maxV ? (v / maxV) * plotH : 0;
    const x = CHART_PAD_L + i * slot + (slot - barW) / 2;
    const y = CHART_PAD_T + plotH - h;
    const clickable = onBarClick && !isSyntheticGroupLabel(g.value);
    const attrs = clickable ? ` class="analytics-bar" data-value="${escapeHtml(g.value)}"` : ' class="analytics-bar-static"';
    const detail = normalize ? ` (avg ${v.toFixed(0)} words/doc)` : "";
    bars +=
      `<rect${attrs} x="${x.toFixed(1)}" y="${y.toFixed(1)}" width="${barW.toFixed(1)}" height="${Math.max(h, 0).toFixed(1)}" rx="2">` +
      `<title>${escapeHtml(String(g.value))} — ${g.document_count} document(s), ${g.segment_count} segment(s), ${g.word_count.toLocaleString()} word(s)${detail}</title>` +
      `</rect>`;
    const lx = x + barW / 2;
    const ly = CHART_H - CHART_PAD_B + 14;
    labels += `<text x="${lx.toFixed(1)}" y="${ly}" text-anchor="end" transform="rotate(-40 ${lx.toFixed(1)} ${ly})">${escapeHtml(truncateLabel(g.value, 16))}</text>`;
  });

  const axis =
    `<line x1="${CHART_PAD_L}" y1="${CHART_PAD_T}" x2="${CHART_PAD_L}" y2="${CHART_PAD_T + plotH}" class="analytics-axis"/>` +
    `<line x1="${CHART_PAD_L}" y1="${CHART_PAD_T + plotH}" x2="${CHART_W - CHART_PAD_R}" y2="${CHART_PAD_T + plotH}" class="analytics-axis"/>`;
  const yLabel = `<text x="2" y="${CHART_PAD_T + 4}" class="analytics-axis-label">${normalize ? "avg words/doc" : "words"}</text>`;

  // Rotated (-40deg) x-axis labels can swing left of x=0 for the leftmost bars -- a
  // negative-origin viewBox gives them room without affecting the plotted geometry above.
  container.innerHTML =
    `<svg viewBox="-30 0 ${CHART_W + 30} ${CHART_H}" width="100%" style="max-width:${CHART_W}px" role="img">` +
    axis + yLabel + bars + labels + `</svg>`;

  if (onBarClick) {
    container.querySelectorAll(".analytics-bar").forEach((el) => {
      el.addEventListener("click", () => onBarClick(el.dataset.value));
    });
  }
}

function renderScatterChart(container, points) {
  if (!points.length) {
    container.innerHTML = '<div class="hint">No data for this breakdown.</div>';
    return;
  }
  const xs = points.map((p) => p.field_value);
  const ys = points.map((p) => p.word_count);
  const minX = Math.min(...xs), maxX = Math.max(...xs);
  const maxY = Math.max(...ys, 1);
  const plotW = CHART_W - CHART_PAD_L - CHART_PAD_R;
  const plotH = CHART_H - CHART_PAD_T - CHART_PAD_B;

  const sx = (v) => CHART_PAD_L + (maxX > minX ? ((v - minX) / (maxX - minX)) * plotW : plotW / 2);
  const sy = (v) => CHART_PAD_T + plotH - (maxY > 0 ? (v / maxY) * plotH : 0);

  const dots = points
    .map(
      (p) =>
        `<circle cx="${sx(p.field_value).toFixed(1)}" cy="${sy(p.word_count).toFixed(1)}" r="4" class="analytics-dot">` +
        `<title>item ${escapeHtml(p.item_id)}: ${p.field_value} → ${p.word_count.toLocaleString()} word(s)</title></circle>`
    )
    .join("");

  const axis =
    `<line x1="${CHART_PAD_L}" y1="${CHART_PAD_T}" x2="${CHART_PAD_L}" y2="${CHART_PAD_T + plotH}" class="analytics-axis"/>` +
    `<line x1="${CHART_PAD_L}" y1="${CHART_PAD_T + plotH}" x2="${CHART_W - CHART_PAD_R}" y2="${CHART_PAD_T + plotH}" class="analytics-axis"/>`;
  const labels =
    `<text x="2" y="${CHART_PAD_T + 4}" class="analytics-axis-label">words</text>` +
    `<text x="${CHART_W - CHART_PAD_R}" y="${CHART_H - 8}" text-anchor="end" class="analytics-axis-label">field value →</text>`;

  container.innerHTML = svgOpen(CHART_W, CHART_H) + axis + labels + dots + `</svg>`;
}

function renderStackedBar(container, groups) {
  if (!groups.length) {
    container.innerHTML = "";
    return;
  }
  const barH = 20, gap = 12, labelW = 150;
  const plotW = CHART_W - labelW - CHART_PAD_R;
  const totalH = groups.length * (barH + gap);

  const rows = groups
    .map((g, i) => {
      const y = i * (barH + gap);
      const sharedW = (g.shared_pct / 100) * plotW;
      const uniqueW = (g.unique_pct / 100) * plotW;
      return (
        `<text x="${labelW - 10}" y="${y + barH / 2 + 4}" text-anchor="end" class="analytics-axis-label">${escapeHtml(truncateLabel(g.value, 20))}</text>` +
        `<rect x="${labelW}" y="${y}" width="${Math.max(sharedW, 0).toFixed(1)}" height="${barH}" class="analytics-vocab-shared">` +
        `<title>${escapeHtml(g.value)}: ${g.shared_pct}% shared with other groups</title></rect>` +
        `<rect x="${(labelW + sharedW).toFixed(1)}" y="${y}" width="${Math.max(uniqueW, 0).toFixed(1)}" height="${barH}" class="analytics-vocab-unique">` +
        `<title>${escapeHtml(g.value)}: ${g.unique_pct}% unique to this group</title></rect>`
      );
    })
    .join("");

  container.innerHTML =
    `<svg viewBox="0 0 ${CHART_W} ${totalH + 8}" width="100%" style="max-width:${CHART_W}px" role="img">${rows}</svg>` +
    `<div class="analytics-vocab-legend">` +
    `<span class="analytics-legend-swatch analytics-vocab-shared"></span> Shared with other groups ` +
    `<span class="analytics-legend-swatch analytics-vocab-unique"></span> Unique to this group` +
    `</div>`;
}

function renderVocabTerms(container, groups) {
  container.innerHTML = groups
    .map(
      (g) =>
        `<div class="analytics-vocab-terms-row"><strong>${escapeHtml(g.value)}</strong>: ` +
        (g.top_unique_terms.length ? g.top_unique_terms.map(escapeHtml).join(", ") : "—") +
        `</div>`
    )
    .join("");
}

// --- Tiles / summary ----------------------------------------------------------------------

function formatTileNumber(n) {
  return n.toLocaleString(undefined, { maximumFractionDigits: 2 });
}

// With zero segments in scope, six zero-value tiles plus an empty "Break down by"
// dropdown and no chart is indistinguishable from a broken page -- show an explicit
// explanation instead (and hide the now-useless breakdown controls/chart/vocab
// sections) so a researcher testing this on a project with no coded/segmented data
// yet -- the common first-run state -- doesn't mistake "nothing to show" for "broken".
function renderAnalyticsEmptyState(isEmpty) {
  document.getElementById("analyticsEmpty").classList.toggle("hidden", !isEmpty);
  document.getElementById("analyticsTiles").classList.toggle("hidden", isEmpty);
  document.getElementById("analyticsBreakdownBar").classList.toggle("hidden", isEmpty);
  if (isEmpty) {
    document.getElementById("analyticsEmpty").innerHTML =
      "No segments are currently in scope for analysis (the same corpus classifier.py trains on). " +
      "This usually means: no data has been imported yet, an imported dataset hasn't been segmented " +
      "(<code>scripts/build_segments.py</code>, or the in-app importer already does this for you), " +
      "or every dataset is currently excluded/unloaded -- see the <strong>Code</strong> tab's Datasets panel.";
    document.getElementById("analyticsChart").innerHTML = "";
    document.getElementById("analyticsVocabOverlap").classList.add("hidden");
    document.getElementById("analyticsShowing").classList.add("hidden");
  }
}

function renderAnalyticsTiles(summary) {
  renderAnalyticsEmptyState(summary.total_segments === 0);
  if (summary.total_segments === 0) return;

  const tiles = [
    ["Documents", formatTileNumber(summary.total_documents)],
    ["Segments", formatTileNumber(summary.total_segments)],
    ["Total words", formatTileNumber(summary.total_word_count)],
    ["Vocabulary size", formatTileNumber(summary.vocabulary_size)],
    ["Lexical diversity", summary.lexical_diversity.toFixed(3)],
    [
      "Avg segment length",
      `${summary.avg_segment_length.toFixed(1)} words (median ${summary.median_segment_length}, ` +
        `range ${summary.min_segment_length}–${summary.max_segment_length})`,
    ],
  ];
  document.getElementById("analyticsTiles").innerHTML = tiles
    .map(
      ([label, value]) =>
        `<div class="analytics-tile"><div class="analytics-tile-label">${escapeHtml(label)}</div>` +
        `<div class="analytics-tile-value">${escapeHtml(String(value))}</div></div>`
    )
    .join("");

  const showingEl = document.getElementById("analyticsShowing");
  if (summary.showing) {
    const fieldMeta = analyticsState.fields.find((f) => f.field === summary.showing.field);
    const label = fieldMeta ? fieldMeta.label : summary.showing.field;
    showingEl.innerHTML =
      `Showing: <strong>${escapeHtml(label)} = ${escapeHtml(summary.showing.value)}</strong> ` +
      `<button id="analyticsClearShowing" class="btn-secondary">Clear</button>`;
    showingEl.classList.remove("hidden");
    document.getElementById("analyticsClearShowing").addEventListener("click", () => loadAnalyticsSummary());
  } else {
    showingEl.innerHTML = "";
    showingEl.classList.add("hidden");
  }
}

async function loadAnalyticsSummary(field, value) {
  const qs = field ? `?field=${encodeURIComponent(field)}&value=${encodeURIComponent(value)}` : "";
  const res = await fetch(`/api/analytics/corpus/summary${qs}`);
  const summary = await res.json();
  renderAnalyticsTiles(summary);
}

// --- Breakdown field selector + chart ------------------------------------------------------

async function loadAnalyticsFields() {
  const res = await fetch("/api/analytics/corpus/fields");
  analyticsState.fields = await res.json();
  const select = document.getElementById("analyticsFieldSelect");
  const previous = analyticsState.selectedField;
  select.innerHTML =
    '<option value="">(none)</option>' +
    analyticsState.fields
      .map(
        (f) =>
          `<option value="${escapeHtml(f.field)}">${escapeHtml(f.label)} (${f.type}, ${f.n_values} value${f.n_values === 1 ? "" : "s"})</option>`
      )
      .join("");
  if (previous && analyticsState.fields.some((f) => f.field === previous)) {
    select.value = previous;
    await onFieldChange();
  }
}

async function onFieldChange() {
  const field = document.getElementById("analyticsFieldSelect").value;
  analyticsState.selectedField = field || null;
  analyticsState.lastBreakdown = null;
  const chartEl = document.getElementById("analyticsChart");
  const vocabEl = document.getElementById("analyticsVocabOverlap");
  const normalizeLabel = document.getElementById("analyticsNormalizeLabel");

  if (!field) {
    chartEl.innerHTML = "";
    vocabEl.classList.add("hidden");
    normalizeLabel.classList.add("hidden");
    return;
  }

  const res = await fetch(`/api/analytics/corpus/breakdown?field=${encodeURIComponent(field)}`);
  const data = await res.json();
  if (analyticsState.selectedField !== field) return; // superseded by a newer selection
  analyticsState.lastBreakdown = data;

  if (data.type === "quantitative") {
    normalizeLabel.classList.add("hidden");
    renderScatterChart(chartEl, data.points);
  } else {
    normalizeLabel.classList.remove("hidden");
    renderBarChart(chartEl, data.groups, {
      normalize: analyticsState.normalize,
      onBarClick: data.type === "categorical" ? (value) => loadAnalyticsSummary(field, value) : null,
    });
  }

  if (data.type === "categorical") {
    const vocabRes = await fetch(`/api/analytics/corpus/vocab_overlap?field=${encodeURIComponent(field)}`);
    const vocab = await vocabRes.json();
    if (analyticsState.selectedField !== field) return;
    renderStackedBar(document.getElementById("analyticsVocabChart"), vocab.groups);
    renderVocabTerms(document.getElementById("analyticsVocabTerms"), vocab.groups);
    vocabEl.classList.remove("hidden");
  } else {
    vocabEl.classList.add("hidden");
  }
}

// --- Events / init --------------------------------------------------------------------

function bindAnalyticsEvents() {
  document.getElementById("analyticsFieldSelect").addEventListener("change", onFieldChange);
  document.getElementById("analyticsNormalize").addEventListener("change", (e) => {
    analyticsState.normalize = e.target.checked;
    if (analyticsState.lastBreakdown && analyticsState.lastBreakdown.type !== "quantitative") {
      renderBarChart(document.getElementById("analyticsChart"), analyticsState.lastBreakdown.groups, {
        normalize: analyticsState.normalize,
        onBarClick:
          analyticsState.lastBreakdown.type === "categorical"
            ? (value) => loadAnalyticsSummary(analyticsState.selectedField, value)
            : null,
      });
    }
  });

  window.onEnterAnalyticsTab = () => {
    loadAnalyticsSummary();
    loadAnalyticsFields();
  };
}

function initAnalytics() {
  bindAnalyticsEvents();
}

initAnalytics();
