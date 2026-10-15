// Adapted for MockX from QuestDB's Apache-2.0 market-depth chart example.
// Original: https://github.com/questdb/blog-examples/tree/main/2026-01-visualizing-market-depth-grafana-questdb-plotly
// Changes: Grafana ArrayVector support, empty-book handling, finite-value filtering,
// meaningful spread gap, linear volume axis and configurable-price wall labels.
function parseArray(val) {
  if (Array.isArray(val)) return val;
  if (typeof val === "string") {
    return val.replace(/[{}]/g, "")
      .split(",")
      .filter(x => x.length > 0)
      .map(Number);
  }
  return [];
}

const NUM_SEGMENTS = 4; // Number of wall segments per side (tweak as you like)
const table = data.series[0];
if (!table) return {data: [], layout: {title: 'Waiting for a complete projected book'}};
function firstValue(field) {
  return typeof field.values.get === 'function' ? field.values.get(0) : field.values[0];
}
const fields = table.fields;

const bprices = fields.find(f => f.name === "bprices");
const bcumvolumes = fields.find(f => f.name === "bcumvolumes");
const bvolumes = fields.find(f => f.name === "bvolumes");
const aprices = fields.find(f => f.name === "aprices");
const acumvolumes = fields.find(f => f.name === "acumvolumes");
const avolumes = fields.find(f => f.name === "avolumes");

if (!bprices || !bcumvolumes || !aprices || !acumvolumes) {
  return {data: [], layout: {title: 'Waiting for depth projection'}};
}

const bps = parseArray(firstValue(bprices));
const bvs = parseArray(firstValue(bcumvolumes));
const brs = bvolumes ? parseArray(firstValue(bvolumes)) : [];
const aps = parseArray(firstValue(aprices));
const avs = parseArray(firstValue(acumvolumes));
const ars = avolumes ? parseArray(firstValue(avolumes)) : [];

const bids = [];
for (let i = 0; i < Math.min(bps.length, bvs.length); i++) {
  if (bps[i] !== null && bvs[i] !== null && Number.isFinite(bps[i]) && Number.isFinite(bvs[i])) bids.push({ x: bps[i], y: bvs[i], raw: brs[i] ?? null });
}

const asks = [];
for (let i = 0; i < Math.min(aps.length, avs.length); i++) {
  if (aps[i] !== null && avs[i] !== null && Number.isFinite(aps[i]) && Number.isFinite(avs[i])) asks.push({ x: aps[i], y: avs[i], raw: ars[i] ?? null });
}

bids.sort((a, b) => b.x - a.x);
asks.sort((a, b) => a.x - b.x);

const mid = bids.length && asks.length ? (bids[0].x + asks[0].x) / 2 : null;
// --- SEGMENTED WALL LOGIC ---
function segmentedWalls(levels, nSegments) {
  const N = levels.length;
  if (N === 0) return [];
  const result = [];
  const segSize = Math.floor(N / nSegments);

  for (let seg = 0; seg < nSegments; seg++) {
    const start = seg * segSize;
    const end = seg === nSegments - 1 ? N : (seg + 1) * segSize;
    let maxIdx = -1, maxRaw = -Infinity;
    for (let i = start; i < end; i++) {
      if (levels[i]?.raw !== undefined && levels[i].raw > maxRaw) {
        maxRaw = levels[i].raw;
        maxIdx = i;
      }
    }
    if (maxIdx >= 0) result.push(levels[maxIdx]);
  }
  return result;
}

const topBids = segmentedWalls(bids, NUM_SEGMENTS);
const topAsks = segmentedWalls(asks, NUM_SEGMENTS);

const wallLines = [...topBids, ...topAsks].map(wall => ({
  type: "line",
  x0: wall.x,
  x1: wall.x,
  y0: 0,
  y1: 1,
  xref: "x",
  yref: "paper",
  line: {
    color: "yellow",
    width: 1,
    dash: "dot"
  }
}));

const wallLabels = [...topBids, ...topAsks].map(wall => ({
  x: wall.x,
  y: 1.02,
  xref: "x",
  yref: "paper",
  text: String(wall.x),
  showarrow: false,
  font: { color: "yellow", size: 10 }
}));

if (bids.length === 0 && asks.length === 0) return {data: [], layout: {title: "Empty book"}};
const prices = [...bids.map(b => b.x), ...asks.map(a => a.x)];
const minX = Math.min(...prices);
const maxX = Math.max(...prices);
const pad = Math.max((maxX - minX) * 0.01, Math.abs(maxX) * 0.00001, 0.0001); // 1% padding

return {
  data: [
    {
      name: "Bids",
      x: bids.map(pt => pt.x),
      y: bids.map(pt => pt.y),
      mode: "lines",
      fill: "tozeroy",
      fillcolor: "rgba(0,255,0,0.2)",
      line: { shape: "hv", color: "rgba(0,255,0,0.7)", width: 2 },
      type: "scatter",
      hovertemplate: "Price: %{x}<br>CumVol: %{y}<extra></extra>"
    },
    {
      name: "Asks",
      x: asks.map(pt => pt.x),
      y: asks.map(pt => pt.y),
      mode: "lines",
      fill: "tozeroy",
      fillcolor: "rgba(255,0,0,0.2)",
      line: { shape: "hv", color: "rgba(255,0,0,0.7)", width: 2 },
      type: "scatter",
      hovertemplate: "Price: %{x}<br>CumVol: %{y}<extra></extra>"
    }
  ],
  layout: {
    plot_bgcolor: "black",
    paper_bgcolor: "black",
    font: { color: "white" },
    xaxis: {
      title: "Price",
      type: "linear",
      showgrid: true,
      gridcolor: "rgba(255,255,255,0.1)",
      zeroline: false,
      range: [minX - pad, maxX + pad]
    },
    yaxis: {
      title: "Cumulative Volume",
      type: "linear",
      showgrid: true,
      gridcolor: "rgba(255,255,255,0.1)",
      zeroline: false
    },
    margin: { t: 20, l: 55, r: 10, b: 30 },
    shapes: [
      ...(mid === null ? [] : [
      {
        type: "line",
        x0: mid,
        x1: mid,
        y0: 0,
        y1: 1,
        xref: "x",
        yref: "paper",
        line: {
          color: "white",
          width: 1,
          dash: "dot"
        }
      }]),
      ...wallLines
    ],
    annotations: wallLabels,
    legend: {
      orientation: "h",
      x: 0.5,
      xanchor: "center",
      y: -0.3
    }
  }
};
