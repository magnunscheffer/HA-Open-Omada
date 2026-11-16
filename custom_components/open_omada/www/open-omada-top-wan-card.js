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

const STRINGS = {
  en: {
    title: "Top WAN usage",
    window: (m) => `Download + upload, last ${m} min`,
    dpiOff: "DPI is turned off on the gateway.",
    empty: "No internet traffic in this period.",
    unavailable: "Entity unavailable.",
    missing: "Entity not found",
    down: "Download",
    up: "Upload",
  },
  "pt-BR": {
    title: "Top consumo de WAN",
    window: (m) => `Download + upload, últimos ${m} min`,
    dpiOff: "O DPI está desligado no gateway.",
    empty: "Sem tráfego de internet no período.",
    unavailable: "Entidade indisponível.",
    missing: "Entidade não encontrada",
    down: "Download",
    up: "Upload",
  },
};

function t(hass) {
  const lang = (hass && (hass.locale?.language || hass.language)) || "en";
  return STRINGS[lang] || STRINGS[lang.split("-")[0]] || STRINGS.en;
}

function formatMb(mb) {
  if (mb >= 1000) return `${(mb / 1000).toFixed(2)} GB`;
  if (mb >= 100) return `${mb.toFixed(0)} MB`;
  return `${mb.toFixed(1)} MB`;
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
    this._config = { max: 5, ...config };
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
    const s = t(this._hass);
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
    if (attrs.window_minutes) sub.textContent = s.window(attrs.window_minutes);
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
      const row = document.createElement("div");
      row.className = "row";
      row.title = `${c.name}\n${s.down}: ${formatMb(Number(c.download_mb) || 0)}\n${s.up}: ${formatMb(Number(c.upload_mb) || 0)}`;

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
      tot.textContent = formatMb(total);

      row.append(rank, main, tot);
      rows.append(row);
    });
    card.append(rows);
  }

  getCardSize() {
    return 1 + Math.ceil((Number(this._config?.max) || 5) * 0.9);
  }

  getGridOptions() {
    return { columns: 6, min_columns: 4, rows: "auto" };
  }

  static getConfigForm() {
    return {
      schema: [
        { name: "entity", required: true, selector: { entity: { domain: "sensor", integration: "open_omada" } } },
        { name: "title", selector: { text: {} } },
        { name: "max", selector: { number: { min: 1, max: 5, mode: "box" } } },
      ],
    };
  }

  static getStubConfig(hass) {
    const entity = Object.keys(hass.states).find(
      (id) => id.startsWith("sensor.") && Array.isArray(hass.states[id].attributes?.top_clients),
    );
    return { entity: entity || "" };
  }
}

if (!customElements.get(CARD_TYPE)) {
  customElements.define(CARD_TYPE, OpenOmadaTopWanCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: CARD_TYPE,
    name: "Open Omada · Top WAN",
    description: "Clients using the most internet traffic (Omada gateway DPI).",
    preview: true,
    documentationURL: "https://github.com/magnunscheffer/HA-Open-Omada",
  });
}
