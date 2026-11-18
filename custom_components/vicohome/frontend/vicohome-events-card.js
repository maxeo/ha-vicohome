// VicoHome events card: recordings with their photo, as a compact list grouped by day or as a grid;
// a tap opens the video. Filters by period and type, "load more" pages back in time.
// Texts in Italian, English and German, from the user's Home Assistant language (English otherwise).
// The header button switches layout (remembered in this browser).
// Data from the websocket command "vicohome/events" (local archive + cloud items).

const I18N = {
  it: {
    k_ring: "Campanello", k_person: "Persona", k_vehicle: "Veicolo", k_pet: "Animale",
    k_package: "Pacco", k_motion: "Movimento", k_live: "Camera avviata",
    p_all: "Tutto il periodo", p_today: "Oggi", p_yesterday: "Ieri", p_24h: "Ultime 24 ore",
    p_7d: "Ultimi 7 giorni", p_30d: "Ultimi 30 giorni", p_day: "Giorno…", p_range: "Intervallo…",
    today: "Oggi", yesterday: "Ieri", all_types: "Tutti i tipi", from: "Dal", to: "Al",
    cloud_only: "Solo nel cloud", as_list: "Mostra come elenco", as_grid: "Mostra come griglia",
    title: "Eventi", error: "Impossibile leggere gli eventi:", loading: "Caricamento…",
    none: "Nessuna registrazione.", none_filtered: "Nessuna registrazione con questi filtri.",
    more: "Carica altri", close: "Chiudi", card_name: "VicoHome: eventi",
    card_desc: "Eventi e live registrate con la loro foto; filtri per periodo e tipo, toccandone uno si apre il video.",
  },
  en: {
    k_ring: "Doorbell", k_person: "Person", k_vehicle: "Vehicle", k_pet: "Pet",
    k_package: "Package", k_motion: "Motion", k_live: "Camera started",
    p_all: "All time", p_today: "Today", p_yesterday: "Yesterday", p_24h: "Last 24 hours",
    p_7d: "Last 7 days", p_30d: "Last 30 days", p_day: "Day…", p_range: "Range…",
    today: "Today", yesterday: "Yesterday", all_types: "All types", from: "From", to: "To",
    cloud_only: "Cloud only", as_list: "Show as list", as_grid: "Show as grid",
    title: "Events", error: "Cannot read the events:", loading: "Loading…",
    none: "No recordings.", none_filtered: "No recordings match these filters.",
    more: "Load more", close: "Close", card_name: "VicoHome: events",
    card_desc: "Events and recorded live views with their photo; filter by period and type, tap one to play the video.",
  },
  de: {
    k_ring: "Türklingel", k_person: "Person", k_vehicle: "Fahrzeug", k_pet: "Haustier",
    k_package: "Paket", k_motion: "Bewegung", k_live: "Kamera gestartet",
    p_all: "Gesamter Zeitraum", p_today: "Heute", p_yesterday: "Gestern", p_24h: "Letzte 24 Stunden",
    p_7d: "Letzte 7 Tage", p_30d: "Letzte 30 Tage", p_day: "Tag…", p_range: "Zeitraum…",
    today: "Heute", yesterday: "Gestern", all_types: "Alle Typen", from: "Von", to: "Bis",
    cloud_only: "Nur in der Cloud", as_list: "Als Liste anzeigen", as_grid: "Als Raster anzeigen",
    title: "Ereignisse", error: "Ereignisse können nicht gelesen werden:", loading: "Wird geladen…",
    none: "Keine Aufnahmen.", none_filtered: "Keine Aufnahmen für diese Filter.",
    more: "Mehr laden", close: "Schließen", card_name: "VicoHome: Ereignisse",
    card_desc: "Ereignisse und aufgenommene Live-Ansichten mit Foto; Filter nach Zeitraum und Typ, Antippen spielt das Video ab.",
  },
};

function pickLang(code) {
  const c = String(code || navigator.language || "en").split("-")[0].toLowerCase();
  return I18N[c] ? c : "en";
}

// [type, label key, color, icon]
const KINDS = [
  ["ring", "k_ring", "#2e7d32", "mdi:bell-ring"],
  ["person", "k_person", "#1565c0", "mdi:account"],
  ["vehicle", "k_vehicle", "#00838f", "mdi:car"],
  ["pet", "k_pet", "#ad1457", "mdi:paw"],
  ["package", "k_package", "#6d4c41", "mdi:package-variant-closed"],
  ["motion", "k_motion", "#546e7a", "mdi:motion-sensor"],
  ["live", "k_live", "#c2702a", "mdi:video"],
];
const PERIODS = [
  ["", "p_all", "mdi:calendar-blank"],
  ["today", "p_today", "mdi:calendar-today"],
  ["yesterday", "p_yesterday", "mdi:calendar-arrow-left"],
  ["24h", "p_24h", "mdi:clock-outline"],
  ["7d", "p_7d", "mdi:calendar-week"],
  ["30d", "p_30d", "mdi:calendar-month"],
  ["day", "p_day", "mdi:calendar-cursor"],
  ["range", "p_range", "mdi:calendar-range"],
];
const COLOR = Object.fromEntries(KINDS.map(([k, , c]) => [k, c]));
const ICON = Object.fromEntries(KINDS.map(([k, , , i]) => [k, i]));

class VicoHomeEventsCard extends HTMLElement {
  setConfig(config) {
    this._config = { limit: 12, columns: 3, layout: "list", ...config };
    this._layout = this._savedLayout() || this._config.layout;
    this._items = null;
    this._more = false;
    this._loading = false;
    this._error = null;
    this._period = ""; // "" = everything, a PERIODS preset, "day" or "range"
    this._d1 = ""; // yyyy-mm-dd: the day, or the start of the range
    this._d2 = ""; // yyyy-mm-dd: end of the range (included)
    this._kind = ""; // "" = all types
    this._loadedAt = 0;
    this._token = 0;
    if (!this.shadowRoot) this.attachShadow({ mode: "open" });
    this._render();
  }

  set hass(hass) {
    const first = !this._hass;
    this._hass = hass;
    const watched = this._watchedState();
    // Reload when the watched event entity changes, and at least every 5 minutes
    // (photo and video URLs are signed for one hour). A refresh keeps the pages already loaded.
    if (first) {
      this._lastWatched = watched;
      this._load("reset");
    } else if (watched !== this._lastWatched || Date.now() - this._loadedAt > 300000) {
      this._lastWatched = watched;
      this._load("refresh");
    }
  }

  _watchedState() {
    const id = this._config.event_entity;
    return id && this._hass.states[id] ? this._hass.states[id].state : null;
  }

  _serial() {
    const cam = this._config.camera && this._hass.states[this._config.camera];
    return cam ? cam.attributes.serial_number : undefined;
  }

  _pageSize() {
    return Math.max(1, Number(this._config.limit) || 12);
  }

  async _load(mode) {
    if (!this._hass) return;
    const token = ++this._token;
    this._loadedAt = Date.now();
    const size = this._pageSize();
    const have = this._items ? this._items.length : 0;
    const want = mode === "refresh" ? Math.max(size, have) : size;
    const msg = { type: "vicohome/events", limit: want + 1, language: this._lang() };
    const serial = this._serial();
    if (serial) msg.serial = serial;
    if (this._kind) msg.kinds = [this._kind];
    const [start, end] = this._range();
    if (start !== undefined) msg.start = start;
    if (end !== undefined) msg.end = end;
    if (mode === "more" && have) msg.before = this._items[have - 1].ts;
    if (mode === "reset") this._items = null;
    this._loading = true;
    this._render();
    try {
      const got = await this._hass.callWS(msg);
      if (token !== this._token) return;
      const page = got.slice(0, want);
      this._more = got.length > want;
      this._items = mode === "more" ? [...(this._items || []), ...page] : page;
      this._error = null;
    } catch (err) {
      if (token !== this._token) return;
      this._error = err.message || String(err);
    }
    this._loading = false;
    this._render();
  }

  _layoutKey() {
    return `vicohome-events-card:${this._config.camera || "all"}:layout`;
  }

  _savedLayout() {
    try {
      const v = localStorage.getItem(this._layoutKey());
      return v === "list" || v === "grid" ? v : null;
    } catch (e) {
      return null;
    }
  }

  _toggleLayout() {
    this._layout = this._layout === "grid" ? "list" : "grid";
    try {
      localStorage.setItem(this._layoutKey(), this._layout);
    } catch (e) {
      /* private mode: the choice lasts until reload */
    }
    this._render();
  }

  _iso(d) {
    return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
  }

  _todayIso() {
    return this._iso(new Date());
  }

  _t(key) {
    return (I18N[pickLang(this._lang())] || I18N.en)[key] ?? I18N.en[key] ?? key;
  }

  _kindLabel(kind) {
    return this._t(`k_${kind}`);
  }

  _lang() {
    return this._hass?.locale?.language || "it";
  }

  _time(iso) {
    return new Date(iso).toLocaleTimeString(this._lang(), { hour: "2-digit", minute: "2-digit", second: "2-digit" });
  }

  _day(iso) {
    const d = new Date(iso);
    const today = new Date();
    const yesterday = new Date(today.getFullYear(), today.getMonth(), today.getDate() - 1);
    const full = d.toLocaleDateString(this._lang(), { day: "numeric", month: "long", year: "numeric" });
    if (d.toDateString() === today.toDateString()) return `${this._t("today")} · ${full}`;
    if (d.toDateString() === yesterday.toDateString()) return `${this._t("yesterday")} · ${full}`;
    const wd = d.toLocaleDateString(this._lang(), { weekday: "long" });
    return `${wd.charAt(0).toUpperCase()}${wd.slice(1)} · ${full}`;
  }

  _short(iso) {
    const d = new Date(iso);
    const time = d.toLocaleTimeString(this._lang(), { hour: "2-digit", minute: "2-digit" });
    if (d.toDateString() === new Date().toDateString()) return `${this._t("today")} ${time}`;
    return `${d.toLocaleDateString(this._lang(), { day: "numeric", month: "short" })} ${time}`;
  }

  _badge(it) {
    return `<span class="badge" style="background:${COLOR[it.kind] || COLOR.motion}">${this._esc(this._kindLabel(it.kind))}</span>`;
  }

  _kicon(it) {
    const icon = ICON[it.kind] || ICON.motion;
    return `<span class="kicon" style="background:${COLOR[it.kind] || COLOR.motion}"><ha-icon icon="${icon}"></ha-icon></span>`;
  }

  _extra(it) {
    return [it.duration ? `${Math.round(it.duration)} s` : "", it.archived ? "" : "☁"].filter(Boolean).join(" · ");
  }

  _listBody() {
    let html = "";
    let day = null;
    this._items.forEach((it, i) => {
      const d = it.time ? this._day(it.time) : "";
      if (d !== day) {
        if (day !== null) html += "</div>";
        day = d;
        html += `<div class="day">${this._esc(d)}</div><div class="rows">`;
      }
      const thumb = it.thumbnail ? `<img loading="lazy" src="${this._esc(it.thumbnail)}" alt="">` : "";
      const extra = this._extra(it);
      html += `<button class="row item" data-i="${i}" ${it.video ? "" : "disabled"}
            title="${it.archived ? "" : this._t("cloud_only")}">
          <span class="mini">${thumb}</span>
          ${this._kicon(it)}
          <span class="txt">
            <span class="tm">${it.time ? this._esc(this._time(it.time)) : ""}</span>
            <span class="sub">${this._badge(it)}${extra ? `<span>${this._esc(extra)}</span>` : ""}</span>
          </span>
          <ha-icon class="chev" icon="mdi:chevron-right"></ha-icon>
        </button>`;
    });
    return html + "</div>";
  }

  _gridBody() {
    const cols = Math.max(1, Math.min(6, Number(this._config.columns) || 3));
    let html = "";
    let day = null;
    this._items.forEach((it, i) => {
      const d = it.time ? this._day(it.time) : "";
      if (d !== day) {
        if (day !== null) html += "</div>";
        day = d;
        html += `<div class="day">${this._esc(d)}</div>
          <div class="grid" style="grid-template-columns: repeat(${cols}, 1fr)">`;
      }
      const thumb = it.thumbnail ? `<img loading="lazy" src="${this._esc(it.thumbnail)}" alt="">` : "";
      const time = it.time
        ? new Date(it.time).toLocaleTimeString(this._lang(), { hour: "2-digit", minute: "2-digit" })
        : "";
      html += `<button class="item cell" data-i="${i}" ${it.video ? "" : "disabled"}>
          ${thumb}
          <span class="tagpos">${this._kicon(it)}${this._badge(it)}</span>
          ${it.archived ? "" : `<ha-icon class="cloud" icon="mdi:cloud-outline" title="${this._t("cloud_only")}"></ha-icon>`}
          ${it.video ? `<ha-icon class="play" icon="mdi:play"></ha-icon>` : ""}
          <span class="shade">
            <span class="ctime">${this._esc(time)}</span>
            ${it.duration ? `<span class="dur">${Math.round(it.duration)} s</span>` : ""}
          </span>
        </button>`;
    });
    return html + "</div>";
  }

  // [start, end) in Unix seconds for the chosen period; undefined = open on that side.
  _range() {
    const now = new Date();
    const midnight = (y, m, d) => new Date(y, m, d).getTime() / 1000;
    const today = midnight(now.getFullYear(), now.getMonth(), now.getDate());
    const day = (iso, plus = 0) => {
      const [y, m, d] = iso.split("-").map(Number);
      return midnight(y, m - 1, d + plus);
    };
    switch (this._period) {
      case "today":
        return [today, undefined];
      case "yesterday":
        return [midnight(now.getFullYear(), now.getMonth(), now.getDate() - 1), today];
      case "24h":
        return [now.getTime() / 1000 - 86400, undefined];
      case "7d":
        return [midnight(now.getFullYear(), now.getMonth(), now.getDate() - 6), undefined];
      case "30d":
        return [midnight(now.getFullYear(), now.getMonth(), now.getDate() - 29), undefined];
      case "day":
        return this._d1 ? [day(this._d1), day(this._d1, 1)] : [undefined, undefined];
      case "range":
        return [this._d1 ? day(this._d1) : undefined, this._d2 ? day(this._d2, 1) : undefined];
      default:
        return [undefined, undefined];
    }
  }

  _toolbar() {
    const dark = this._hass?.themes?.darkMode;
    const today = new Date();
    const max = this._iso(today);
    const period = PERIODS.find(([v]) => v === this._period) || PERIODS[0];
    const kind = KINDS.find(([k]) => k === this._kind);
    return `<div class="tools" style="color-scheme:${dark ? "dark" : "light"}">
        <button class="pick period ${this._period ? "on" : ""}">
          <ha-icon icon="${period[2]}"></ha-icon><span>${this._t(period[1]).replace("…", "")}</span>
          <ha-icon class="caret" icon="mdi:menu-down"></ha-icon></button>
        ${this._period === "day"
          ? `<label class="field"><input type="date" class="d1" value="${this._d1}" max="${max}"></label>`
          : ""}
        ${this._period === "range"
          ? `<label class="field" title="${this._t("from")}"><input type="date" class="d1" value="${this._d1}" max="${this._d2 || max}"></label>
             <span class="dash">–</span>
             <label class="field" title="${this._t("to")}"><input type="date" class="d2" value="${this._d2}" min="${this._d1}" max="${max}"></label>`
          : ""}
        ${this._period ? `<button class="iconbtn clear" title="${this._t("p_all")}"><ha-icon icon="mdi:close"></ha-icon></button>` : ""}
        <button class="pick kind kindf ${kind ? "on" : ""}">
          ${kind
            ? `<span class="dotico" style="background:${kind[2]}"><ha-icon icon="${kind[3]}"></ha-icon></span>`
            : `<ha-icon icon="mdi:filter-variant"></ha-icon>`}
          <span>${kind ? this._t(kind[1]) : this._t("all_types")}</span>
          <ha-icon class="caret" icon="mdi:menu-down"></ha-icon></button>
        <button class="iconbtn switch" title="${this._layout === "grid" ? this._t("as_list") : this._t("as_grid")}">
          <ha-icon icon="${this._layout === "grid" ? "mdi:view-list" : "mdi:view-grid"}"></ha-icon></button>
      </div>`;
  }

  _render() {
    if (!this.shadowRoot || !this._config) return;
    const title = this._config.title ?? this._t("title");
    let body;
    if (this._error) {
      body = `<div class="msg">${this._t("error")} ${this._esc(this._error)}</div>`;
    } else if (!this._items) {
      body = `<div class="msg">${this._t("loading")}</div>`;
    } else if (!this._items.length) {
      body = `<div class="msg">${this._t(this._period || this._kind ? "none_filtered" : "none")}</div>`;
    } else {
      body = this._layout === "list" ? this._listBody() : this._gridBody();
      if (this._more) {
        body += `<button class="more" ${this._loading ? "disabled" : ""}>${this._t(this._loading ? "loading" : "more")}</button>`;
      }
    }
    this.shadowRoot.innerHTML = `
      <style>
        ha-card { padding: 12px; }
        .head { font-size: 1.1em; font-weight: 500; margin: 2px 4px 8px; }
        .tools { display: flex; align-items: center; gap: 6px; margin: 0 0 4px; flex-wrap: wrap; }
        .field { display: flex; align-items: center; gap: 4px; padding: 2px 8px; border-radius: 8px;
                 background: var(--secondary-background-color); color: var(--secondary-text-color);
                 --mdc-icon-size: 18px; min-height: 32px; box-sizing: border-box; }
        .field input, .field select { all: unset; font: inherit; font-size: 13px; color: var(--primary-text-color);
                 cursor: pointer; }
        .field select option { color: initial; }
        .kindf { margin-right: auto; }
        .dash { color: var(--secondary-text-color); }
        ha-card { position: relative; }
        .pick { all: unset; box-sizing: border-box; cursor: pointer; display: flex; align-items: center; gap: 6px;
                padding: 2px 4px 2px 10px; border-radius: 8px; min-height: 32px; font-size: 13px;
                background: var(--secondary-background-color); color: var(--primary-text-color);
                border: 1px solid transparent; --mdc-icon-size: 18px; }
        .pick > ha-icon:first-child { color: var(--secondary-text-color); }
        .pick:hover, .pick:focus-visible { border-color: var(--divider-color); }
        .pick.on { border-color: var(--primary-color); }
        .pick .caret { color: var(--secondary-text-color); --mdc-icon-size: 20px; }
        .dotico { width: 20px; height: 20px; border-radius: 50%; display: inline-flex; align-items: center;
                  justify-content: center; color: #fff; --mdc-icon-size: 13px; flex: none; }
        .menu { position: absolute; z-index: 5; min-width: 200px; padding: 6px; border-radius: 12px;
                background: var(--card-background-color, #1c1c1c); box-shadow: 0 8px 28px rgba(0,0,0,.45);
                border: 1px solid var(--divider-color); display: flex; flex-direction: column; gap: 1px; }
        .opt { all: unset; box-sizing: border-box; cursor: pointer; display: flex; align-items: center; gap: 10px;
               padding: 8px 10px; border-radius: 8px; font-size: 14px; color: var(--primary-text-color);
               --mdc-icon-size: 20px; }
        .opt .dotico { width: 24px; height: 24px; --mdc-icon-size: 15px; }
        .opt .lead { color: var(--secondary-text-color); width: 24px; display: flex; justify-content: center; }
        .opt .ol { flex: 1; }
        .opt:hover, .opt:focus-visible { background: var(--secondary-background-color); }
        .opt.sel { background: rgba(var(--rgb-primary-color, 3, 169, 244), .15); }
        .opt .check { color: var(--primary-color); }
        .tools .switch { margin-left: auto; }
        .iconbtn { all: unset; cursor: pointer; padding: 4px; border-radius: 50%; display: flex;
                   color: var(--secondary-text-color); }
        .iconbtn:hover { background: var(--secondary-background-color); }
        .badge { color: #fff; font-size: 11px; font-weight: 600; line-height: 16px; padding: 0 6px;
                 border-radius: 4px; white-space: nowrap; }
        .day { font-weight: 600; font-size: 13px; margin: 12px 4px 2px; color: var(--secondary-text-color); }
        .row { all: unset; box-sizing: border-box; cursor: pointer; display: flex; align-items: center; gap: 12px;
               width: 100%; padding: 6px 4px; border-radius: 8px; }
        .row:hover { background: var(--secondary-background-color); }
        .row[disabled] { cursor: default; opacity: .6; }
        .row:focus-visible, .cell:focus-visible { outline: 2px solid var(--primary-color); }
        .mini { width: 88px; height: 50px; border-radius: 6px; overflow: hidden; flex: none;
                background: var(--divider-color); }
        .mini img { width: 100%; height: 100%; object-fit: cover; display: block; }
        .txt { flex: 1; min-width: 0; display: flex; flex-direction: column; gap: 4px; }
        .tm { font-size: 14px; font-weight: 500; font-variant-numeric: tabular-nums; }
        .sub { display: flex; align-items: center; gap: 8px; font-size: 12px; color: var(--secondary-text-color); }
        .chev { color: var(--secondary-text-color); --mdc-icon-size: 20px; flex: none; }
        .grid { display: grid; gap: 8px; margin-top: 6px; }
        .cell { all: unset; cursor: pointer; border-radius: 8px; overflow: hidden; position: relative;
                display: block; aspect-ratio: 16 / 9; background: var(--divider-color); }
        .cell[disabled] { cursor: default; opacity: .6; }
        .cell img { width: 100%; height: 100%; object-fit: cover; display: block; transition: transform .2s; }
        .cell:hover img { transform: scale(1.04); }
        .kicon { width: 30px; height: 30px; border-radius: 50%; flex: none; display: flex; align-items: center;
                 justify-content: center; color: #fff; --mdc-icon-size: 18px; }
        .tagpos { position: absolute; left: 6px; top: 6px; display: flex; align-items: center; gap: 5px; }
        .tagpos .kicon { width: 24px; height: 24px; --mdc-icon-size: 15px; box-shadow: 0 0 0 2px rgba(0,0,0,.35); }
        .cloud { position: absolute; right: 6px; top: 5px; color: #fff; --mdc-icon-size: 16px;
                 filter: drop-shadow(0 0 2px #000); }
        .play { position: absolute; left: 50%; top: 50%; transform: translate(-50%, -50%); color: #fff;
                --mdc-icon-size: 22px; background: rgba(0,0,0,.45); border-radius: 50%; padding: 6px;
                opacity: 0; transition: opacity .15s; }
        .cell:hover .play, .cell:focus-visible .play { opacity: 1; }
        .shade { position: absolute; left: 0; right: 0; bottom: 0; display: flex; justify-content: space-between;
                 align-items: flex-end; padding: 16px 8px 5px; color: #fff; font-size: 12px;
                 background: linear-gradient(transparent, rgba(0,0,0,.75)); font-variant-numeric: tabular-nums; }
        .ctime { font-weight: 600; font-size: 13px; }
        .dur { opacity: .85; }
        .more { all: unset; box-sizing: border-box; cursor: pointer; display: block; width: 100%; text-align: center;
                margin-top: 8px; padding: 8px; border-radius: 8px; color: var(--primary-color);
                background: var(--secondary-background-color); font-weight: 500; }
        .more[disabled] { cursor: default; opacity: .7; }
        .msg { padding: 12px 4px; color: var(--secondary-text-color); }
        .modal { position: fixed; inset: 0; background: rgba(0,0,0,.8); display: flex; align-items: center;
                 justify-content: center; z-index: 10; padding: 16px; }
        .box { width: min(960px, 100%); background: var(--card-background-color); border-radius: 12px; overflow: hidden; }
        .box video { width: 100%; display: block; background: #000; max-height: 75vh; }
        .bar { display: flex; align-items: center; gap: 8px; padding: 10px 12px; }
        .bar .t { flex: 1; display: flex; align-items: center; gap: 8px; }
        .bar button { all: unset; cursor: pointer; padding: 4px 10px; border-radius: 8px;
                      background: var(--secondary-background-color); }
      </style>
      <ha-card>
        ${title ? `<div class="head">${this._esc(title)}</div>` : ""}
        ${this._toolbar()}
        ${body}
      </ha-card>`;
    const $ = (sel) => this.shadowRoot.querySelector(sel);
    $(".switch").addEventListener("click", () => this._toggleLayout());
    $(".period").addEventListener("click", (e) =>
      this._menu(
        e.currentTarget,
        PERIODS.map(([v, l, i]) => ({ value: v, label: this._t(l), icon: i })),
        this._period,
        (v) => this._setPeriod(v)
      )
    );
    $(".d1")?.addEventListener("change", (e) => {
      this._d1 = e.target.value;
      this._load("reset");
    });
    $(".d2")?.addEventListener("change", (e) => {
      this._d2 = e.target.value;
      this._load("reset");
    });
    $(".kind").addEventListener("click", (e) =>
      this._menu(
        e.currentTarget,
        [{ value: "", label: this._t("all_types"), icon: "mdi:filter-variant-remove" }].concat(
          KINDS.map(([k, l, c, i]) => ({ value: k, label: this._t(l), icon: i, color: c }))
        ),
        this._kind,
        (v) => {
          this._kind = v;
          this._load("reset");
        }
      )
    );
    $(".clear")?.addEventListener("click", () => {
      this._period = "";
      this._load("reset");
    });
    $(".more")?.addEventListener("click", () => this._load("more"));
    this.shadowRoot.querySelectorAll(".item").forEach((el) =>
      el.addEventListener("click", () => this._open(this._items[Number(el.dataset.i)]))
    );
  }

  _setPeriod(v) {
    this._period = v;
    if (v === "day" && !this._d1) this._d1 = this._todayIso();
    if (v === "range" && !this._d2) {
      this._d2 = this._todayIso();
      const d = new Date();
      d.setDate(d.getDate() - 6);
      this._d1 = this._iso(d);
    }
    this._load("reset");
  }

  // Popup menu in the style of Home Assistant's selects: icon, label, check on the chosen one.
  _menu(anchor, options, current, onPick) {
    if (this._closeMenu) {
      this._closeMenu();
      return;
    }
    const card = this.shadowRoot.querySelector("ha-card");
    const menu = document.createElement("div");
    menu.className = "menu";
    menu.setAttribute("role", "listbox");
    menu.innerHTML = options
      .map((o, i) => {
        const lead = o.color
          ? `<span class="dotico" style="background:${o.color}"><ha-icon icon="${o.icon}"></ha-icon></span>`
          : `<ha-icon class="lead" icon="${o.icon}"></ha-icon>`;
        const sel = o.value === current;
        return `<button class="opt ${sel ? "sel" : ""}" role="option" aria-selected="${sel}" data-i="${i}">
            ${lead}<span class="ol">${this._esc(o.label)}</span>${sel ? `<ha-icon class="check" icon="mdi:check"></ha-icon>` : ""}
          </button>`;
      })
      .join("");
    const a = anchor.getBoundingClientRect();
    const c = card.getBoundingClientRect();
    menu.style.left = `${Math.max(0, Math.min(a.left - c.left, c.width - 210))}px`;
    menu.style.top = `${a.bottom - c.top + 4}px`;
    card.appendChild(menu);
    const close = () => {
      menu.remove();
      document.removeEventListener("pointerdown", outside, true);
      document.removeEventListener("keydown", onKey, true);
      this._closeMenu = null;
    };
    const outside = (e) => {
      const path = e.composedPath();
      if (!path.includes(menu) && !path.includes(anchor)) close();
    };
    const onKey = (e) => {
      const opts = [...menu.querySelectorAll(".opt")];
      const at = opts.indexOf(this.shadowRoot.activeElement);
      if (e.key === "Escape") close();
      else if (e.key === "ArrowDown") opts[Math.min(opts.length - 1, at + 1)].focus();
      else if (e.key === "ArrowUp") opts[Math.max(0, at - 1)].focus();
      else return;
      e.preventDefault();
    };
    menu.querySelectorAll(".opt").forEach((el) =>
      el.addEventListener("click", () => {
        close();
        onPick(options[Number(el.dataset.i)].value);
      })
    );
    document.addEventListener("pointerdown", outside, true);
    document.addEventListener("keydown", onKey, true);
    this._closeMenu = close;
    (menu.querySelector(".sel") || menu.querySelector(".opt")).focus();
  }

  _open(item) {
    if (!item || !item.video) return;
    const modal = document.createElement("div");
    modal.className = "modal";
    const secs = item.duration ? ` · ${Math.round(item.duration)} s` : "";
    modal.innerHTML = `<div class="box">
        <video controls autoplay playsinline src="${this._esc(item.video)}" poster="${this._esc(item.thumbnail || "")}"></video>
        <div class="bar"><span class="t">${this._kicon(item)}${this._badge(item)}<span>${this._esc(item.time ? this._short(item.time) : "")}${secs}</span></span>
          <button class="close">${this._t("close")}</button></div>
      </div>`;
    const close = () => {
      modal.querySelector("video")?.pause();
      modal.remove();
      document.removeEventListener("keydown", onKey);
    };
    const onKey = (e) => e.key === "Escape" && close();
    modal.addEventListener("click", (e) => e.target === modal && close());
    modal.querySelector(".close").addEventListener("click", close);
    document.addEventListener("keydown", onKey);
    this.shadowRoot.appendChild(modal);
  }

  _esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  getCardSize() {
    const n = this._items?.length || this._pageSize();
    if (this._layout === "list") return 3 + n;
    return 3 + Math.ceil(n / (this._config?.columns || 3)) * 2;
  }

  static getStubConfig(hass) {
    const camera = Object.keys(hass.states).find(
      (id) => id.startsWith("camera.") && hass.states[id].attributes.serial_number
    );
    const event = Object.keys(hass.states).find((id) => id.startsWith("event.") && id.includes("attivita"));
    return { camera, event_entity: event, limit: 12, layout: "list", columns: 3 };
  }
}

// Language for the card picker: the frontend's, read once when the file loads.
const CARD_LANG = pickLang(document.querySelector("home-assistant")?.hass?.locale?.language);

if (!customElements.get("vicohome-events-card")) {
  customElements.define("vicohome-events-card", VicoHomeEventsCard);
  window.customCards = window.customCards || [];
  window.customCards.push({
    type: "vicohome-events-card",
    name: I18N[CARD_LANG].card_name,
    description: I18N[CARD_LANG].card_desc,
    preview: false,
  });
}
