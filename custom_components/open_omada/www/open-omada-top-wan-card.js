/*
 * Open Omada · Top WAN card
 * Horizontal bar ranking of the clients using the most internet traffic, read from the
 * `top_clients` attribute of the Open Omada "Top WAN client" sensor (gateway DPI).
 *
 * Shipped and registered by the open_omada integration; no manual resource needed.
 * Client names come from the controller: they are always written with textContent.
 */

const CARD_TYPE = "open-omada-top-wan-card";
const NOT_RENDERED = Symbol("not-rendered");

// All card texts are English on purpose (project convention).
const S = {
  title: "Top WAN usage",
  rateSub: (w) => `Average speed, last ${w}`,
  totalSub: (w) => `Download + upload, last ${w}`,
  dpiOff: "DPI is turned off on the gateway.",
  empty: "No internet traffic in this period.",
  unavailable: "Entity unavailable.",
  missing: "Entity not found",
  down: "Download",
  up: "Upload",
};

function formatWindow(minutes) {
  if (minutes >= 60 && minutes % 60 === 0) return `${minutes / 60} h`;
  return `${minutes} min`;
}

function formatMb(mb) {
  if (mb >= 1000) return `${(mb / 1000).toFixed(2)} GB`;
  if (mb >= 100) return `${mb.toFixed(0)} MB`;
  return `${mb.toFixed(1)} MB`;
}

function formatRate(mbps) {
  if (mbps >= 1000) return `${(mbps / 1000).toFixed(2)} Gbit/s`;
  if (mbps >= 10) return `${mbps.toFixed(0)} Mbit/s`;
  return `${mbps.toFixed(1)} Mbit/s`;
}

const STYLE = `
  ha-card { padding: 16px; }
  .header { display: flex; align-items: baseline; justify-content: space-between; gap: 8px;
            margin-bottom: 12px; }
  .title { font-size: var(--ha-card-header-font-size, 1.25rem); font-weight: 500;
           color: var(--ha-card-header-color, var(--primary-text-color)); }
  .sub { font-size: 0.8rem; color: var(--secondary-text-color); white-space: nowrap; }
  .rows { display: flex; flex-direction: column; gap: 10px; }
  .row { display: grid; grid-template-columns: 1.5em minmax(0, 1fr) auto; align-items: center;
         column-gap: 8px; cursor: default; }
  .rank { font-size: 0.85rem; color: var(--secondary-text-color); text-align: right; }
  .main { min-width: 0; }
  .name { font-size: 0.95rem; color: var(--primary-text-color); overflow: hidden;
          text-overflow: ellipsis; white-space: nowrap; margin-bottom: 4px; }
  .track { height: 8px; border-radius: 4px; background: var(--divider-color, rgba(127,127,127,.2));
           overflow: hidden; }
  .bar { height: 100%; border-radius: 4px; background: var(--primary-color);
         transition: width .4s ease; }
  .row:first-child .bar { background: var(--accent-color, var(--primary-color)); }
  .total { font-size: 0.95rem; font-weight: 500; color: var(--primary-text-color);
           font-variant-numeric: tabular-nums; text-align: right; min-width: 4.5em; }
  .msg { color: var(--secondary-text-color); font-size: 0.95rem; padding: 4px 0; }
  .warn { color: var(--error-color); }
`;

class OpenOmadaTopWanCard extends HTMLElement {
  setConfig(config) {
    if (!config || !config.entity) {
      throw new Error("Set 'entity' to an Open Omada 'Top WAN client' sensor");
    }
    this._config = { max: 5, display: "auto", ...config };
    this._lastState = NOT_RENDERED;
    if (!this.shadowRoot) {
      this.attachShadow({ mode: "open" });
      const style = document.createElement("style");
      style.textContent = STYLE;
      this._card = document.createElement("ha-card");
      this.shadowRoot.append(style, this._card);
    }
    this._render();
  }

  set hass(hass) {
    this._hass = hass;
    const state = hass.states[this._config?.entity];
    if (state === this._lastState) return; // only re-render when the entity object changed
    this._lastState = state;
    this._render();
  }

  _render() {
    if (!this._card || !this._config) return;
    const s = S;
    const state = this._hass?.states[this._config.entity];
    const card = this._card;
    card.replaceChildren();

    const header = document.createElement("div");
    header.className = "header";
    const title = document.createElement("div");
    title.className = "title";
    title.textContent = this._config.title ?? s.title;
    const sub = document.createElement("div");
    sub.className = "sub";
    header.append(title, sub);
    card.append(header);

    const msg = (text, warn = false) => {
      const el = document.createElement("div");
      el.className = warn ? "msg warn" : "msg";
      el.textContent = text;
      card.append(el);
    };

    if (!this._hass) return;
    if (!state) return msg(`${s.missing}: ${this._config.entity}`, true);
    if (state.state === "unavailable") return msg(s.unavailable);
    const attrs = state.attributes || {};
    const minutes = Number(attrs.window_minutes) || 0;
    const mode = this._config.display === "auto" ? (minutes && minutes <= 15 ? "rate" : "total") : this._config.display;
    const asRate = mode === "rate";
    if (minutes) sub.textContent = (asRate ? s.rateSub : s.totalSub)(formatWindow(minutes));
    if (attrs.dpi_enabled === false) return msg(s.dpiOff);

    const clients = (Array.isArray(attrs.top_clients) ? attrs.top_clients : [])
      .filter((c) => c && Number(c.total_mb) > 0)
      .slice(0, Number(this._config.max) || 5);
    if (!clients.length) return msg(s.empty);

    const max = Math.max(...clients.map((c) => Number(c.total_mb)));
    const rows = document.createElement("div");
    rows.className = "rows";
    clients.forEach((c, i) => {
      const total = Number(c.total_mb);
      const rate = Number(c.rate_mbps ?? (minutes ? (total * 8) / (minutes * 60) : 0));
      const row = document.createElement("div");
      row.className = "row";
      row.title = `${c.name}\n${s.down}: ${formatMb(Number(c.download_mb) || 0)}\n${s.up}: ${formatMb(Number(c.upload_mb) || 0)}\nTotal: ${formatMb(total)}`;

      const rank = document.createElement("div");
      rank.className = "rank";
      rank.textContent = String(i + 1);

      const main = document.createElement("div");
      main.className = "main";
      const name = document.createElement("div");
      name.className = "name";
      name.textContent = String(c.name ?? c.mac ?? "?");
      const track = document.createElement("div");
      track.className = "track";
      const bar = document.createElement("div");
      bar.className = "bar";
      bar.style.width = `${Math.max(2, (total / max) * 100)}%`;
      track.append(bar);
      main.append(name, track);

      const tot = document.createElement("div");
      tot.className = "total";
      tot.textContent = asRate ? formatRate(rate) : formatMb(total);

      row.append(rank, main, tot);
      rows.append(row);
    });
    card.append(rows);
  }

  getCardSize() {
    return 1 + Math.ceil((Number(this._config?.max) || 5) * 0.9);
  }

  getGridOptions() {
    return { columns: 12, min_columns: 6, rows: "auto" };
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { domain: "sensor", integration: "open_omada" } } },
        { name: "title", selector: { text: {} } },
        { name: "max", selector: { number: { min: 1, max: 5, mode: "box" } } },
        {
          name: "display",
          selector: { select: { mode: "dropdown", options: [
            { value: "auto", label: "Auto (speed for short windows, total otherwise)" },
            { value: "rate", label: "Average speed (Mbit/s)" },
            { value: "total", label: "Total (MB/GB)" },
          ] } },
        },
      ],
    };
  }

  static getStubConfig(hass) {
    const ids = Object.keys(hass.states).filter(
      (id) => id.startsWith("sensor.") && Array.isArray(hass.states[id].attributes?.top_clients),
    );
    const entity = ids.find((id) => Number(hass.states[id].attributes.window_minutes) <= 15) || ids[0];
    return { entity: entity || "" };
  }
}

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, OpenOmadaTopWanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TYPE,
    name: "Open Omada · Top WAN",
    description: "Clients using the most internet traffic: last 10 min (speed) or 24 h (total), from the Omada gateway DPI.",
    preview: true,
    documentationURL: "https://github.com/magnunscheffer/HA-Open-Omada",
  });
}
