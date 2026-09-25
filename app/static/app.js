"use strict";
const ui = {
  mode: localStorage.getItem("gi-mode") || "simulation",
  view: "calls",
  calls: null,
  callsError: "",
  call: null,
  behind: false,
  sourcing: null,
  sourcingError: "",
  trails: {},
  asks: {},
  askDraft: {},  // a question half typed, by person: kept when an answer arrives and the page redraws
  scorecard: null,
  scorecardError: "",
  events: null,
  eventsError: "",
  week: null,
  weekError: "",
  hiring: null,
  hiringError: "",
  hiringSaving: false,
  selected: null,
  role: "all",
  query: "",
  state: null,
  sequence: null,
  sequenceError: "",
  crustdata: null,
  crustdataError: "",
  busy: false,
  error: "",
  dirty: false,
};
const $ = (s) => document.querySelector(s);
const esc = (v) =>
  String(v ?? "").replace(
    /[&<>"']/g,
    (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" })[
        c
      ],
  );
const list = (v) => (Array.isArray(v) ? v : []);
const human = (v) => String(v || "unknown").replaceAll("_", " ");
const url = (v) => {
  try {
    const u = new URL(v);
    return ["https:", "http:"].includes(u.protocol) ? u.href : "#";
  } catch {
    return "#";
  }
};
const date = (v) => {
  if (!v) return "Not known";
  if (/^\d{4}$/.test(String(v))) return esc(v); // a year or a month is all the source gives
  if (/^\d{4}-\d{2}$/.test(String(v)))
    return new Date(v + "-15T12:00:00").toLocaleDateString(undefined, { month: "short", year: "numeric" });
  const d = new Date(
    /^\d{4}-\d{2}-\d{2}$/.test(String(v)) ? v + "T12:00:00" : v,
  );
  return Number.isNaN(d.valueOf())
    ? esc(v)
    : d.toLocaleDateString(undefined, {
        month: "short",
        day: "numeric",
        year: "numeric",
      });
};
const day = (v) => date(v && String(v).slice(0, 10)); // readiness days are UTC midnights: show the day itself
const stamp = (v) => {
  if (!v) return "Not known";
  const d = new Date(v);
  return Number.isNaN(d.valueOf())
    ? esc(v)
    : d.toLocaleString(undefined, {
        month: "short",
        day: "numeric",
        hour: "numeric",
        minute: "2-digit",
        timeZoneName: "short",
      });
};
const badge = (v, label) =>
  `<span class="badge ${esc(v)}">${esc(label || human(v))}</span>`;
const button = (text, action, cls = "", attrs = "") =>
  `<button type="button" class="button ${cls}" data-action="${action}" ${attrs}>${text}</button>`;
const profileLink = (c) =>
  `<a class="button" href="${esc(url(c.profile_url))}" target="_blank" rel="noopener noreferrer">Open profile ↗</a>`;
const roleTitle = (id) =>
  ui.state?.roles?.find((r) => r.id === id)?.title || id || "Unassigned";
const current = () => ui.state?.candidates?.find((c) => c.id === ui.selected);
const eventFor = (c) =>
  list(c.events).find((e) => e.id === c.decision?.event_id) ||
  list(c.events)[0];
const priority = (v) =>
  typeof v === "number"
    ? v
    : { urgent: 4, high: 3, routine: 2, low: 1 }[v] || 0;
const score = (v) =>
  v === null || v === undefined
    ? "Unknown"
    : typeof v === "number"
      ? String(Math.round(v * 100) / 100)
      : esc(v);
async function api(path, method = "GET", body) {
  const res = await fetch(path, {
    method,
    headers: body ? { "Content-Type": "application/json" } : {},
    body: body ? JSON.stringify(body) : undefined,
  });
  let data;
  try {
    data = await res.json();
  } catch {
    throw new Error(
      `Server returned ${res.status}. Check the application terminal.`,
    );
  }
  if (!res.ok)
    throw new Error(
      typeof data.detail === "string"
        ? data.detail
        : typeof data.error === "string"
          ? data.error
          : JSON.stringify(data.detail || data.error || data),
    );
  return data;
}
let toastTimer;
function toast(message, error = false) {
  const el = $("#toast");
  el.textContent = message;
  el.className = `visible${error ? " error" : ""}`;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (el.className = ""), error ? 9000 : 4500);
}
async function refresh(force = false) {
  const requestedMode = ui.mode;
  try {
    const data = await api(`/api/state?mode=${requestedMode}`);
    if (requestedMode !== ui.mode) return;
    ui.state = data;
    const recovered = Boolean(ui.error); // a banner from a failed Refresh goes as soon as the engine answers again
    ui.error = "";
    if (ui.view === "sequence") {
      try {
        const sequence = await api(`/api/sequence?mode=${requestedMode}`);
        if (requestedMode !== ui.mode) return;
        ui.sequence = sequence;
        ui.sequenceError = "";
      } catch (error) {
        ui.sequenceError = error.message;
        ui.sequence = null;
      }
    }
    let fetched = false;
    for (const [view, key, path] of ENGINE_VIEWS) {
      // The engine runs once per view and on Refresh: never again on the poll, while it runs, or after an error.
      if (ui.view !== view || (!force && (ui[key] || ui[key + "Error"] || ui[key + "Loading"]))) continue;
      ui[key + "Loading"] = true;
      fetched = true;
      try {
        const data = await api(`${path}?mode=${requestedMode}`);
        if (requestedMode !== ui.mode) return;
        ui[key] = data;
        ui[key + "Error"] = "";
        if (key === "calls") [ui.sourcing, ui.trails, ui.sourcingError] = [null, {}, ""]; // a new run: read what came in again
      } catch (error) {
        ui[key] = null;
        ui[key + "Error"] = error.message;
      } finally {
        ui[key + "Loading"] = false;
      }
    }
    if (["sequence", "setup"].includes(ui.view) && requestedMode === "live") {
      try {
        const feed = await api("/api/crustdata");
        if (requestedMode !== ui.mode) return;
        ui.crustdata = feed;
        ui.crustdataError = "";
      } catch (error) {
        ui.crustdata = null;
        ui.crustdataError = error.message;
      }
    }
    // Replacing the DOM closes disclosures/native menus and can discard answers
    // while someone reads, so a poll repaints only an untouched page.
    // An engine page changes only when its data comes back: the poll never resets its unsaved choices.
    if (
      force ||
      ((fetched || recovered || (!ENGINE_VIEWS.some(([view]) => view === ui.view) && ui.view !== "board")) && // the board preview has no data
        !ui.dirty &&
        !$("#dialog").open &&
        !$("#main details[open]") &&
        !["INPUT", "TEXTAREA", "SELECT"].includes(
          document.activeElement?.tagName,
        ))
    )
      render();
    if (fetched) await loadSourcing();
  } catch (e) {
    ui.error = e.message;
    if (!ui.state || force) render();
  }
}
// Today's calls and the ops pages read one ledger, plan and New starts: after a change anywhere (the candidate pages
// write to the same ledger), every other one loads afresh when next opened; the page on screen reloads with
// refresh(true). ``all`` clears it too (a reset).
function opsChanged(all = false) {
  for (const [view, key] of ENGINE_VIEWS) {
    if (key !== "scorecard" && (all || view !== ui.view)) {
      ui[key] = null;
      ui[key + "Error"] = "";
    }
  }
}
// The pages that run the engine: [view, ui key, API path]. Each loads once, and again on Refresh.
const ENGINE_VIEWS = [["calls", "calls", "/api/calls"], ["week", "week", "/api/week"], ["scorecard", "scorecard", "/api/scorecard"], ["events", "events", "/api/events"], ["hiring", "hiring", "/api/hiring"], ["starts", "starts", "/api/starts"], ["month", "month", "/api/month"]];
// The engine's pages. The older candidate pipeline (its own practice people, not the engine's) folds under "Older
// pages": still reachable, never mistaken for the engine.
const NAV = [["calls", "Today's calls"], ["week", "Monday brief"], ["scorecard", "Scorecard"], ["events", "Events"], ["hiring", "Hiring budget"], ["starts", "New starts"], ["month", "Monthly one-pager"], ["roles", "Role briefs"], ["setup", "Setup"]];
const OLDER = [["sequence", "Watches & alerts"], ["inbox", "Opportunity inbox"], ["watchlist", "Watchlist"], ["sources", "Sources & runs"]];
const isOlder = (view) => OLDER.some(([id]) => id === view);
function nav() {
  const item = ([id, name]) =>
    `<button class="nav-button ${ui.view === id ? "active" : ""}" data-view="${id}" ${ui.view === id ? 'aria-current="page"' : ""}>${name}${["inbox", "watchlist"].includes(id) ? `<span class="nav-count">${getCandidates(id).length}</span>` : ""}</button>`;
  const open = ui.olderOpen || isOlder(ui.view);
  return `<aside class="sidebar"><div class="brand"><div class="brand-mark">GI</div><div><span>Timing engine</span><small>Research & recruiting</small></div></div><nav aria-label="Main navigation">${NAV.map(item).join("")}<button class="nav-button nav-group" data-action="toggle-older" aria-expanded="${open}" ${isOlder(ui.view) ? "disabled" : ""}>Older pages<span class="nav-count">${open ? "▾" : "▸"}</span></button>${open ? `<div class="nav-older"><p class="nav-note">The earlier candidate pipeline, with its own practice people. Not the timing engine.</p>${OLDER.map(item).join("")}</div>` : ""}</nav><div class="nav-bottom"><strong>Every ping needs a reason.</strong><br>Evidence, a moment, and a useful conversation.<br><br><span class="status-dot"></span>${ui.state ? "Connected to local engine" : "Connecting…"}</div></aside>`;
}
function getCandidates(view = ui.view) {
  return list(ui.state?.candidates)
    .filter(
      (c) =>
        view !== "inbox" ||
        ["ready", "needs_review"].includes(c.decision?.state),
    )
    .filter((c) => ui.role === "all" || c.role_id === ui.role)
    .filter((c) =>
      `${c.name} ${c.employer} ${c.summary}`
        .toLowerCase()
        .includes(ui.query.toLowerCase()),
    );
}
function render() {
  const names = {
    calls: "Today's calls",
    week: "Monday brief",
    scorecard: "Scorecard",
    events: "Events",
    hiring: "Hiring budget",
    starts: "New starts",
    month: "Monthly one-pager",
    sequence: "Watches & alerts",
    inbox: "Opportunity inbox",
    watchlist: "Watchlist",
    roles: "Role briefs",
    board: "New job posts (preview)",
    sources: "Sources & runs",
    setup: "Setup",
  };
  $("#app").innerHTML =
    `<div class="shell">${nav()}<div class="workspace"><header class="topbar"><span class="breadcrumb">General Intuition / ${names[ui.view]}</span>${ui.view === "board" ? "" : `<div class="mode-switch" aria-label="Data mode"><button data-mode="simulation" class="${ui.mode === "simulation" ? "selected" : ""}">Simulation</button><button data-mode="live" class="${ui.mode === "live" ? "selected" : ""}">Live data</button></div>`}</header>${ui.view === "board" && ui.mode === "live" ? "" : `<div class="mode-banner ${ui.mode === "live" ? "live" : "simulation"}"><span>${ui.mode === "simulation" ? "<strong>Simulation: invented data.</strong> Every person, post, event, amount and contact on these pages is made up to show how the engine decides. Live data has the real watchlist." : "<strong>Live workspace.</strong> Real research requires source verification and human review."}</span><span>Outreach is drafted; never automatically sent.</span></div>`}${isOlder(ui.view) ? '<div class="older-note"><strong>An older page.</strong> The earlier candidate pipeline, with its own practice people and model judgment. The timing engine\'s calls are on Today\'s calls.</div>' : ""}${ui.error ? `<div class="connection-error">${esc(ui.error)} ${button("Retry", "refresh")}</div>` : ""}<main id="main">${!ui.state ? `<div class="empty"><h2>${ui.error ? "The engine is unavailable" : "Loading workspace…"}</h2><p>${ui.error ? "Make sure the local server is running, then retry." : "Fetching role briefs and opportunity evidence."}</p></div>` : ui.view === "calls" ? callsPage() : ui.view === "week" ? weekPage() : ui.view === "scorecard" ? scorecardPage() : ui.view === "events" ? eventsPage() : ui.view === "hiring" ? hiringPage() : ui.view === "starts" ? startsPage() : ui.view === "month" ? monthPage() : ["inbox", "watchlist"].includes(ui.view) ? reviewPage() : ui.view === "sequence" ? sequencePage() : ui.view === "roles" ? rolesPage() : ui.view === "board" ? boardPage() : ui.view === "sources" ? sourcesPage() : setupPage()}</main></div></div>`;
  if (ui.view === "sequence") requestAnimationFrame(acknowledgeRenderedSequence);
}
const pendingSequenceReceipts = new Set();
const confirmedSequenceReceipts = new Set();
function acknowledgeRenderedSequence() {
  if (ui.view !== "sequence" || !ui.sequence || document.hidden) return;
  const rendered = new Set([...document.querySelectorAll("[data-sequence-alert-id]")].map((node) => node.dataset.sequenceAlertId));
  const ids = list(ui.sequence.alerts).filter((alert) => alert.status === "proposed" && !alert.seen_at && rendered.has(alert.id) && !pendingSequenceReceipts.has(alert.id) && !confirmedSequenceReceipts.has(alert.id)).map((alert) => alert.id);
  if (!ids.length) return;
  ids.forEach((id) => pendingSequenceReceipts.add(id));
  api("/api/sequence/receipts", "POST", { alert_ids: ids }).then(() => {
    ids.forEach((id) => confirmedSequenceReceipts.add(id));
    ui.sequenceReceiptError = "";
    // The server timestamp arrives on the next refresh. Do not trigger another render.
  }).catch((error) => {
    ui.sequenceReceiptError = `Browser display receipt failed: ${error.message}`;
  }).finally(() => ids.forEach((id) => pendingSequenceReceipts.delete(id)));
}
function heading(title, description, actions = "") {
  return `<div class="page-heading"><div><h1>${title}${ui.mode === "simulation" ? ' <span class="badge invented">Invented data</span>' : ""}</h1><p>${description}</p></div><div class="actions">${actions}</div></div>`;
}
// When live data was pulled, and a warning when it is old or the last run failed: a broken pull must never read as a
// quiet week. The simulation's banner already says its data is invented.
function freshLine(f) {
  if (!f || ui.mode !== "live") return "";
  return f.warning ? `<div class="callout warning fresh-line"><strong>Check the pull first.</strong> ${esc(f.warning)} <span class="small">${esc(f.line)}</span></div>` : `<p class="small muted fresh-line">${esc(f.line)}</p>`;
}
const CALL_BADGE = { reach_now: "reach", verify_first: "verify", watch_until: "wait", respect_follow_up: "wait", quiet: "quiet" };
const callBadge = (c) => badge(CALL_BADGE[c.action] || "quiet", c.until ? `${c.headline} until ${date(c.until)}` : c.headline);
const kindBadge = (c) => {  // what a reach is about (happened.kind), and any public moment of the last month
  const m = list(c.happened)[0];
  return (c.kind === "early_sign" ? badge("early", "Early sign") : "") + (m ? badge("happened", `Just happened · ${m.label}`) : "");
};
const source = (href, text = "Source") => href ? `<a href="${esc(url(href))}" target="_blank" rel="noopener noreferrer">${esc(text)} ↗</a>` : '<span class="muted">No link</span>';
// The company watcher's newest read (app/companies.py): a preview beside the calls, folded so the calls come first.
function companiesPanel(p) {
  if (!p) return "";
  if (p.missing) return `<div class="content"><details class="panel"><summary>Company moments: no read yet</summary><p class="small">${esc(p.missing)}</p></details></div>`;
  const rows = list(p.companies);
  const company = (c) => `<div class="company-moment"><h3>${esc(c.company)} ${badge(c.stance === "Open" ? "reach" : c.stance === "Held" ? "suppressed" : "verify", c.stance)}</h3>${c.why ? `<p class="small">${esc(c.why)}</p>` : ""}<ul class="reason-list">${list(c.moments).map((m) => `<li><strong>${esc(m.label)}</strong> · ${date(m.day)}: ${esc(m.quote)} ${source(m.source_url)}${m.headlines > 1 ? ` <span class="muted">and ${m.headlines - 1} more ${m.headlines === 2 ? "headline" : "headlines"}</span>` : ""}</li>`).join("")}</ul>${list(c.people).length ? `<p class="small">Watched here: ${c.people.map((w) => `${esc(w.name)} (${esc(w.role)}; today: ${esc(w.call)})`).join(", ")}</p>` : ""}${list(c.authors).length ? `<p class="small">Authors on GI's work here: ${c.authors.map((a) => `${esc(a.name)} (${a.papers})`).join(", ")}</p>` : ""}</div>`;
  return `<div class="content"><details class="panel"><summary>Company moments: ${rows.length} ${rows.length === 1 ? "company" : "companies"}, ${date(p.since)} to ${date(p.read_on)}</summary><p class="small">What just happened at the companies GI hires from, and who GI would want there. A preview: nothing is sent and no one's call changes. A company GI works with is held, and one GI sells to asks sales first.</p>${rows.map(company).join("") || '<p class="muted">Nothing happened at a watched company this month.</p>'}${list(p.errors).length ? `<p class="small muted">Not read: ${p.errors.map(esc).join("; ")}</p>` : ""}</details></div>`;
}
function callsPage() {
  const data = ui.calls;
  const refreshButton = button(ui.behind ? "Hide behind the scenes" : "Behind the scenes", "behind-toggle", ui.behind ? "selected" : "", `aria-pressed="${ui.behind}"`) + button("Refresh", "calls-refresh");
  if (!data) return `${heading("Today's calls", "Who to reach now, who to check first, who to wait for, and who to leave alone.", refreshButton)}<div class="content"><div class="panel"><p>${ui.callsError ? esc(ui.callsError) : "Running the engine…"}</p></div></div>`;
  const calls = list(data.calls);
  let c = calls.find((x) => x.subject_id === ui.call);
  if (!c && calls.length && window.innerWidth > 620) c = calls[0];
  const counts = Object.entries(calls.reduce((n, x) => ({ ...n, [x.headline]: (n[x.headline] || 0) + 1 }), {})).map(([h, n]) => `${n} ${esc(h.toLowerCase())}`).join(" · ");
  return `${heading("Today's calls", data.missing ? "No timelines to judge yet." : `${esc(data.source)}. ${calls.length ? counts + "." : "No people in it yet."}`, refreshButton)}${data.missing ? "" : `<div class="fresh-strip">${freshLine(data.fresh)}${ui.behind ? behindPanel(data) : ""}</div>${companiesPanel(data.companies)}`}${data.missing ? `<div class="content"><div class="panel"><h2>Nothing to judge yet</h2><p>${esc(data.missing)}</p><p class="footnote">Switch to Simulation to see the engine on invented people.</p></div></div>` : `<div class="review-grid ${c ? "has-selection" : ""}"><section class="queue" aria-label="Today's calls"><div class="queue-heading"><span>${calls.length} ${calls.length === 1 ? "person" : "people"}</span><span>As of ${date(data.as_of)}</span></div>${calls.map((x) => `<button class="candidate ${x === c ? "selected" : ""}" data-call="${esc(x.subject_id)}" aria-pressed="${x === c}"><div class="candidate-name">${esc(x.name)}</div><div class="candidate-role">${esc(x.role.title)}${x.employer ? "<br>" + esc(x.employer) : ""}</div><div class="row"><span class="badges">${callBadge(x)}${kindBadge(x)}${x.gate ? badge("suppressed", GATE[x.gate.state][0]) : ""}</span><span class="small muted">${x.trigger ? date(x.trigger.date) : ""}</span></div><div class="candidate-excerpt">${esc(x.trigger ? x.trigger.what : x.why_now)}</div></button>`).join("")}</section><article class="detail">${c ? pingDetail(c, data) : `<div class="empty"><h2>Pick a person</h2><p>Each call opens into its full ping.</p></div>`}</article></div>`}`;
}
// Behind the scenes (app/sourcing.py): what the engine watches and what came in, then one person's trail from each
// public item to what was read off it, what the engine makes of it, and the call. Loaded only while it is shown.
const whoIsShown = () => ui.call || list(ui.calls?.calls)[0]?.subject_id;
async function loadSourcing() {
  const mode = ui.mode, who = whoIsShown();
  if (!ui.behind || ui.view !== "calls" || !ui.calls || ui.calls.missing) return;
  const jobs = [];
  if (!ui.sourcing) jobs.push(api(`/api/calls/sources?mode=${mode}`).then((d) => { if (mode === ui.mode) ui.sourcing = d; }));
  if (who && !ui.trails[who]) jobs.push(api(`/api/calls/${encodeURIComponent(who)}/trail?mode=${mode}`).then((d) => { if (mode === ui.mode) ui.trails[who] = d; }));
  if (!jobs.length) return;
  try {
    await Promise.all(jobs);
    if (mode === ui.mode) ui.sourcingError = "";
  } catch (e) {
    if (mode === ui.mode) ui.sourcingError = e.message;
  }
  if (mode === ui.mode) render();
}
function behindPanel(data) {
  const o = ui.sourcing;
  if (!o) return `<div class="panel behind"><p class="small">${esc(ui.sourcingError || "Reading what the engine watches…")}</p></div>`;
  const calls = list(data.calls), reach = calls.filter((c) => c.action === "reach_now" && !c.gate).length;  // held reaches get no card
  const flow = [[`${o.items} public items`, `for ${o.people} people watched`], ["Read for early signs", "work, asks, GI's topics"], [`${calls.length} calls`, "reach, check, wait or quiet"], [`${reach} to reach`, "a card with a checked draft"]];
  const kinds = `<div class="table-scroll"><table><thead><tr><th>Source</th><th>People</th><th>Items</th><th>Newest public</th>${o.live ? "<th>Stored on the last pull</th>" : ""}</tr></thead><tbody>${list(o.kinds).map((k) => `<tr><td>${esc(k.label)}</td><td>${esc(k.people)}</td><td>${esc(k.items)}</td><td>${day(k.newest)}</td>${o.live ? `<td>${esc(k.new ?? "n/a")}</td>` : ""}</tr>`).join("")}</tbody></table></div>`;
  const runs = !o.live ? "" : list(o.runs).length ? `<h3>The daily run, newest first</h3><div class="table-scroll"><table><thead><tr><th>Ran</th><th>Status</th><th>Records pulled</th><th>Model reads</th><th>Cost</th><th>Slack</th></tr></thead><tbody>${o.runs.map((r) => `<tr><td>${stamp(r.at)}</td><td>${esc(human(r.status))}</td><td>${esc(r.pulled ?? "n/a")}</td><td>${esc(r.read ?? "n/a")}</td><td>${typeof r.usd === "number" ? "$" + r.usd.toFixed(3) : "n/a"}</td><td>${r.posted ? "Posted" : "Nothing posted"}</td></tr>`).join("")}</tbody></table></div>` : '<p class="small muted">No daily run on record yet.</p>';
  return `<div class="panel behind"><div class="row"><h2>Behind the scenes</h2><span class="small muted">${esc(o.where)}</span></div><ol class="flow">${flow.map(([a, b]) => `<li><strong>${esc(a)}</strong><span>${esc(b)}</span></li>`).join("")}</ol><p class="small">${o.live ? "Public sources, and GI's own notes where the table lists them. Each morning the daily run pulls the watchlist's new public posts and code, and news and layoff filings about their employers; papers come from research builds. The post reader reads each new item for early signs of work in GI's area, and the engine makes the calls below. Nothing is sent from here." : "Invented people and items: no pull ran. On live data the daily run pulls the watchlist's new public posts and code, and news and filings about their employers, every morning."}</p>${kinds}${runs}<p class="small muted">Pick a person to see the trail from each item to their call.</p></div>`;
}
const ENGINE_SAYS = {
  counts: (e) => `Counts toward the call: ${e.what}${e.day ? `, open until ${day(e.day)}` : ""}`,
  fading: (e) => `Counts toward the call as it fades: ${e.what}, its window closed ${day(e.day)}`,
  hold: (e) => `Holds the call: ${e.what}${e.day ? `, until ${day(e.day)}` : ""}`,
  context: (e) => `${e.what}: listed, never a reason`,
  waits: (e) => `${e.what}, from ${day(e.day)}: the call waits until then`,
  later: (e) => `${e.what}, from ${day(e.day)}`,
  closed: (e) => `${e.what}: seen, ${e.day ? `its window closed ${day(e.day)}` : "not counted now"}`,
  seen: (e) => `${e.what}: seen, not counted now`,
  aside: (e) => e.day ? `Not counted: the call counts the newer ${e.what} from ${day(e.day)}` : "Not counted toward the call",
};
const WHOSE = { employer: "about their employer", gi: "GI's own", "someone close to them": "about someone close to them" };
const ON_CALL = { trigger: "<strong>The trigger</strong> (part 3)", listed: "One of the signals behind the call", counted: "Counts toward the call", holds: "Holds the call back", waits: "The call waits for it" };
function behindTrail(c) {
  const t = ui.trails[c.subject_id];
  if (!t) return `<section class="section behind-trail"><p class="small">${esc(ui.sourcingError || "Tracing where this call came from…")}</p></section>`;
  const watched = list(t.watched).map((w) => `<li>${w.url ? source(w.url, w.label) : esc(w.label)} <span class="small muted">${w.items ? `${esc(w.items)} ${w.items === 1 ? "item" : "items"}, newest ${day(w.newest)}` : "nothing stored yet"}</span></li>`).join("");
  const step = (title, body) => `<div class="trail-step"><span class="trail-label">${title}</span>${body}</div>`;
  const item = (i) => `<li class="trail-item ${i.on_call ? "on-call" : ""}" data-read="${list(i.read).length}" data-is="${esc(i.is)}">${step(esc(i.label), `<p class="small">${i.is ? `<strong>${esc(i.is)}</strong> · ` : ""}Public ${day(i.public)}${i.stored ? ` · pulled ${stamp(i.stored)}` : ""}${WHOSE[i.whose] ? ` · ${esc(WHOSE[i.whose])}` : ""}</p>${i.quote ? `<blockquote>${esc(i.quote)}</blockquote>` : ""}<p class="small">${source(i.url)}</p>`)}${step("Read as", list(i.read).length ? `<p class="small">${esc(i.read.join(" · "))}</p>` : '<p class="small muted">Nothing read off it</p>')}${step("The engine", `${list(i.engine).length ? `<ul class="reason-list small">${i.engine.map((e) => `<li class="engine-${esc(e.state)}">${esc(ENGINE_SAYS[e.state]?.(e) || e.what)}</li>`).join("")}</ul>` : '<p class="small muted">Not a reason</p>'}${i.moment ? `<p class="small"><strong>Public moment:</strong> ${esc(i.moment)}</p>` : ""}`)}${step("On the call", `<p class="small" data-on-call="${esc(i.on_call)}">${ON_CALL[i.on_call] || '<span class="muted">No</span>'}</p>`)}</li>`;
  const items = list(t.items), behind = items.slice(0, t.behind), rest = items.slice(t.behind), holds = list(t.call.holds);
  return `<section class="section behind-trail"><div class="section-title"><h3>How this call was sourced</h3><span class="small muted">${esc(t.call.headline)}${list(t.call.reasons).length ? ": " + esc(t.call.reasons.join("; ")) : ""}</span></div>${holds.length ? `<p class="small" data-holds="${esc(holds.join("|"))}">Held back by ${esc(holds.join("; "))}</p>` : ""}<p class="small"><strong>Watched for ${esc(t.name.split(" ")[0])}</strong></p><ul class="reason-list">${watched || '<li class="muted">No public profile on file.</li>'}</ul><p class="small"><strong>Behind the call</strong>, newest first</p>${behind.length ? `<ol class="trail">${behind.map(item).join("")}</ol>` : '<p class="small muted">Nothing in view is behind it.</p>'}${rest.length ? `<p class="small"><strong>The rest in view</strong>, newest first</p><ol class="trail">${rest.map(item).join("")}</ol>` : ""}${t.more ? `<p class="small muted">${esc(t.more)} older ${t.more === 1 ? "item" : "items"} in view, none behind the call.</p>` : ""}${t.hidden ? `<p class="small muted" data-hidden="${esc(t.hidden)}">${esc(t.hidden)} more ${t.hidden === 1 ? "post or code item" : "posts and code items"} in view had nothing read off ${t.hidden === 1 ? "it" : "them"}, so ${t.hidden === 1 ? "it isn't" : "they aren't"} shown.</p>` : ""}</section>`;
}
// Ask about this person (app/ask.py): the suggested questions are answered free from what the engine holds; any other
// goes to the model, which answers only from the same facts. Every line cites the items behind it.
const SUGGESTED = [["jd", "Match to the job description"], ["team", "Closeness to GI's team"], ["shipped", "What they shipped lately"], ["way_in", "The way in"]];
function askPanel(c) {
  const first = c.name.split(" ")[0];
  const cite = (a, n) => { const s = list(a.sources).find((x) => x.n === n); return s?.url ? ` <a class="cite" href="${esc(url(s.url))}" target="_blank" rel="noopener noreferrer" title="${esc(s.label)}, ${esc(s.date)}">[${esc(n)}]</a>` : ` <span class="cite">[${esc(n)}]</span>`; };
  const how = (a) => a.pending || a.error ? "" : a.free ? "from the stored data, free" : `${a.model}, ${a.cached ? "asked before, free" : `$${Number(a.usd).toFixed(3)}`}`;
  const answer = (a) => `<div class="answer"><p class="small"><strong>${esc(a.question)}</strong> <span class="muted">${esc(how(a))}</span></p>${a.error ? `<p class="small">${esc(a.error)}</p>` : a.pending ? '<p class="small muted">Reading what the engine holds…</p>' : `<ul class="reason-list">${list(a.lines).map((l) => `<li>${esc(l.text)}${list(l.cites).map((n) => cite(a, n)).join("")}</li>`).join("")}</ul>`}</div>`;
  return `<section class="section ask"><div class="section-title"><h3>Ask about ${esc(first)}</h3><span class="small muted">Answers come only from what's stored for them, with the items behind each line</span></div><div class="actions ask-chips">${SUGGESTED.map(([k, label]) => button(label, "ask-suggested", "", `data-key="${k}" data-subject="${esc(c.subject_id)}"`)).join("")}</div><form class="ask-form" data-subject="${esc(c.subject_id)}"><input name="question" maxlength="500" required value="${esc(ui.askDraft[c.subject_id] || "")}" placeholder="Anything else about ${esc(first)}? Answered by the model from the same data, a few cents a question" aria-label="Ask about ${esc(first)}"><button class="button" type="submit">Ask</button></form>${list(ui.asks[c.subject_id]).map(answer).join("")}</section>`;
}
async function askAbout(subject, body, question) {
  const mode = ui.mode, asked = (ui.asks[subject] = list(ui.asks[subject]));
  if (asked.some((a) => a.pending && a.question === question)) return;  // a second click on the same question
  const entry = { question, pending: true };
  asked.unshift(entry);
  render();
  try {
    Object.assign(entry, await api(`/api/calls/${encodeURIComponent(subject)}/ask`, "POST", { mode, ...body }), { pending: false });
  } catch (e) {
    Object.assign(entry, { pending: false, error: e.message });
  }
  if (mode === ui.mode) render();
}
const GATE = { hold: ["Held", "Held by the contact ledger."], check_first: ["Confirm first", "The contact ledger asks for a check first."], draft: ["Fix the draft", "Don't send this draft yet."] };
function pingPart(n, title, body) {
  return `<section class="section ping-part"><div class="section-title"><h3><span class="step-number">${n}</span>${title}</h3></div>${body}</section>`;
}
function pingDetail(c, data) {
  const t = c.trigger, d = c.draft, r = c.route;
  const held = c.gate ? `<p class="hold-note"><strong>${GATE[c.gate.state][1]}</strong> ${esc(c.gate.reason)}</p>` : "";
  const drafted = d ? `<div class="draft">${d.to ? `<div class="draft-meta"><span>To: ${esc(d.to)}</span></div>` : ""}${d.subject ? `<div class="draft-subject">${esc(d.subject)}</div>` : ""}<div class="draft-body">${esc(d.body)}</div></div>${c.gate ? "" : `<div class="actions" style="margin-top:10px">${button("Copy draft", "copy-call-draft")}${r?.url ? `<a class="button" href="${esc(url(r.url))}" target="_blank" rel="noopener noreferrer">${esc(r.open)} ↗</a>` : ""}</div>`}<p class="footnote">Nothing is sent from here. A person reads it, edits it and sends it.</p>` : `<p class="muted">No draft: ${c.action === "verify_first" ? "check the fact first." : c.action ? "the call is not to reach out yet." : "nothing to write about yet."}</p>`;
  const routed = r ? `${r.sender ? `<p><strong>From ${esc(r.sender)}</strong> <span class="small muted">${esc(r.sender_why || "")}</span></p>` : ""}${r.ask ? `<p><strong>Ask ${esc(r.ask.to)} on Slack first</strong>: they've worked with ${esc(c.name.split(" ")[0])}.</p><div class="draft"><div class="draft-body">${esc(r.ask.body)}</div></div><p class="small muted">The draft in part 6 goes with it.</p>` : ""}<p><strong>${esc(r.channel[0].toUpperCase() + r.channel.slice(1))}</strong>${r.target ? " · " + esc(r.target) : ""}</p><p class="small">${esc(r.reason)}</p>${list(r.paths).length ? `<ul class="reason-list">${r.paths.map((p) => `<li>${esc(p)}</li>`).join("")}</ul>` : `<p class="small">Warm path: none found.${data.team_loaded ? "" : " No GI team list is loaded, so nobody's ties were checked."}</p>`}` : `<p class="muted">${c.action === "reach_now" ? "None found." : "Not looked for until the call is to reach out."}</p>`;
  return `${button("‹ Back to calls", "calls-back", "mobile-back")}<div class="person-heading"><div><h2>${esc(c.name)}</h2><p>${esc(c.employer || "")}</p></div><div class="actions">${callBadge(c)}${kindBadge(c)}</div></div>${c.track ? `<p class="detail-summary">${esc(c.track)}.</p>` : ""}${held}${ui.behind ? behindTrail(c) : ""}${askPanel(c)}
${pingPart(1, "Person", `<p>${esc(c.name)}${c.employer ? ", " + esc(c.employer) : ""}</p><p>${list(c.profiles).length ? c.profiles.map((p) => source(p.url, p.label)).join(" · ") : c.profile_url ? `${source(c.profile_url, "Public profile")} <span class="small muted">(not yet tied to them by an identity check)</span>` : '<span class="muted">No public profile link on file</span>'}</p><p class="small">${c.email ? `Email: ${esc(c.email.address)}${c.email.source_url ? " · listed on " + source(c.email.source_url) : ""}` : "Email: no public email found."}</p>`)}
${pingPart(2, "Role", `<p>${c.role.jd_url ? source(c.role.jd_url, c.role.title) : esc(c.role.title)}</p>${c.role.pay ? `<p class="small">${esc(c.role.pay)}</p>` : ""}`)}
${pingPart(3, "Trigger", t ? `<p><strong>${date(t.date)}</strong> · ${esc(t.what)}</p>${t.quote ? `<blockquote>${esc(t.quote)}</blockquote>` : ""}<p class="small">${source(t.source_url)}</p>` : `<p class="muted">${c.action ? `No reason open as of ${date(data.as_of)}.` : "Nothing of their own is in view."}</p>`)}
${pingPart(4, "Why now", `${c.kind === "early_sign" ? "<p><strong>Early sign.</strong> The reach rests on an early sign of their own work.</p>" : ""}${list(c.happened).length ? `<p><strong>Just happened</strong> (public in the last month):</p><ul class="reason-list">${c.happened.map((m) => `<li>${esc(m.line)}${m.source_url ? " · " + source(m.source_url) : ""}</li>`).join("")}</ul>` : ""}<p>${esc(c.why_now)}</p><p><strong>Next:</strong> ${esc(c.next_step)}</p>`)}
${pingPart(5, "Confidence, and what would prove it wrong", c.confidence ? `<p><strong>${esc(c.confidence[0].toUpperCase() + c.confidence.slice(1))}.</strong> <span class="small muted">A label from the engine's rules, not a measured rate. The Scorecard page measures how early signs did against random timing.</span></p>${list(c.falsifiers).length ? `<ul class="reason-list">${c.falsifiers.map((f) => `<li>${esc(f)}</li>`).join("")}</ul>` : '<p class="muted">Nothing recorded.</p>'}` : '<p class="muted">No call yet.</p>')}
${pingPart(6, "Draft outreach", `${c.message ? `<p><strong>Message:</strong> ${esc(c.message)}</p>` : ""}${drafted}`)}
${pingPart(7, "Route in", routed)}
${list(c.signals).length > 1 ? `<section class="section"><div class="section-title"><h3>Every signal behind the call</h3></div>${c.signals.map((s) => `<div class="evidence-item"><div class="row"><strong>${esc(s.what)}</strong><span class="small muted">${date(s.date)}</span></div>${!s.quote ? "" : `<p class="small">${/^\(not quoted/.test(s.quote) ? esc(s.quote) : `“${esc(s.quote)}”`}</p>`}<p class="small">${source(s.source_url)}</p></div>`).join("")}</section>` : ""}`;
}
const pct = (v) => (typeof v === "number" ? `${Math.round(v * 100)}%` : "n/a");
const weeks = (v) => (typeof v === "number" ? `${esc(v)} weeks` : "n/a");
// A win over random timing is claimed only at p under 0.05: random timing is the pooled rate, so a higher rate before
// moments always "beats" it, and on a few moments that is often luck.
function scorecardVerdict(engine, random, keepP = 0.05) {
  if (engine.moments < 5) return `Too few public moments to tell yet: ${engine.moments} among the scored people.`;
  const seen = engine.seen_rate ?? 0, rs = random.seen_rate ?? 0, fa = engine.false_alarm_rate ?? 0, rf = random.false_alarm_rate ?? 0;
  const counts = `${engine.seen_coming} of ${engine.moments} moments against ${engine.fired_with_nothing_after} of ${engine.ordinary_stretches} ordinary stretches`;
  const p = typeof engine.p === "number" ? engine.p : null;
  if (seen <= rs) return `The engine did not beat random timing: ${counts}.`;
  if (p === null || p >= keepP) return `Not distinguishable from random timing yet: ${counts}${p === null ? "" : `, p ${p.toFixed(2)}`}.`;
  if (fa <= rf) return `The engine saw more moments coming than random timing did, without more false alarms: ${counts}, p ${p.toFixed(3)}.`;
  return `The engine saw more moments coming than random timing did, but raised more false alarms: ${counts}, p ${p.toFixed(3)}.`;
}
function scorecardPage() {
  const head = heading("Scorecard", "Did early signs come before people's public moments, and did the engine beat random timing and waiting for the news?", button("Refresh", "scorecard-refresh"));
  if (ui.mode === "simulation") return `${head}<div class="content"><div class="panel"><h2>Only on real people</h2><p>The scorecard replays the engine week by week before each person's public moment (a paper, a launch, job news) and against their ordinary stretches. On invented people it would prove nothing, so it runs only on Live data.</p></div></div>`;
  const report = ui.scorecard?.report;
  if (!report) return `${head}<div class="content"><div class="panel"><h2>No scorecard yet</h2><p>${esc(ui.scorecardError || "Run it on the Mac after the pull and the post reader: uv run python scripts/posts.py scorecard --people <the people file>.")}</p><p class="footnote">It writes a report to ${esc(ui.scorecard?.folder || "research/private/early-signals")}; this page shows the newest one.</p></div></div>`;
  const cards = report.scorecard, all = cards.all_signs, kept = cards.kept_signs, random = all.random_timing;
  const row = (name, e) => `<tr><td>${name}</td><td>${esc(e.seen_coming)} of ${esc(e.moments)} (${pct(e.seen_rate)})</td><td>${weeks(e.lead_weeks?.median)}</td><td>${pct(e.false_alarm_rate)}${typeof e.fired_with_nothing_after === "number" ? ` (${esc(e.fired_with_nothing_after)} of ${esc(e.ordinary_stretches)})` : ""}</td><td>${typeof e.p === "number" ? e.p.toFixed(2) : "n/a"}</td></tr>`;
  const news = all.obvious_news;
  const signs = Object.entries(report.noise.signs);
  const keepP = report.config?.keep?.[1] ?? 0.05;
  const noneKept = !signs.some(([, s]) => s.early && s.kept);
  return `${head}<div class="content"><div class="panel"><h2>${esc(scorecardVerdict(all.engine, random, keepP))}</h2><p class="small">From ${esc(report.file)}: the noise test ran on ${list(report.halves.noise_test).length} people and the scorecard on the other ${list(report.halves.scorecard).length}. Small numbers: p is the chance a gap over random timing this big is luck, and a gap is only claimed at p under ${esc(keepP)}.</p><div class="table-scroll"><table><thead><tr><th>How GI would have reached out</th><th>Moments seen coming</th><th>Median lead</th><th>False alarms</th><th>p</th></tr></thead><tbody>${row("The engine, on early signs", all.engine)}${noneKept ? '<tr><td>The engine, only signs that passed the noise test</td><td colspan="4">n/a: no sign was kept</td></tr>' : row("The engine, only signs that passed the noise test", kept.engine)}<tr><td>Random timing</td><td>${pct(random.seen_rate)}</td><td>${weeks(random.median_lead_weeks)}</td><td>${pct(random.false_alarm_rate)}</td><td></td></tr><tr><td>Waiting for the news</td><td>${pct(news.seen_rate)}</td><td>${weeks(news.lead_weeks)}</td><td>${pct(news.false_alarm_rate)}</td><td></td></tr>${row("Any reach-out, including on public news", all.any_reach)}</tbody></table></div><p class="footnote">Seen coming: the engine said reach out on an early sign (work in progress, a public ask, a submission) 1 to 7 weeks before the moment. False alarms: the same people's ordinary 8-week stretches, with nothing public in them or after, where it said the same. Random timing deals the engine's own weekly calls to random stretches. Waiting for the news is never early and never wrong, and arrives with everyone else.</p></div><div class="panel"><h2>Noise test</h2><p class="small">Each sign in the 8 weeks before a moment against the same people's ordinary stretches. A sign is kept when it is at least twice as common before a moment, at p under 0.05.${list(report.dropped).length ? " Not kept, not yet shown to beat an ordinary stretch: " + esc(report.dropped.map((s) => report.noise.signs[s]?.label || s).join("; ")) + "." : ""}</p><div class="table-scroll"><table><thead><tr><th>Sign</th><th>Before a moment</th><th>Ordinary stretches</th><th>Lift</th><th>p</th><th>Kept</th></tr></thead><tbody>${signs.map(([id, s]) => `<tr><td>${esc(s.label || id)}</td><td>${esc(s.before_moment)} of ${esc(s.before_moment_windows)} (${pct(s.before_moment_rate)})</td><td>${esc(s.ordinary)} of ${esc(s.ordinary_windows)} (${pct(s.ordinary_rate)})</td><td>${typeof s.lift === "number" ? s.lift.toFixed(1) : "n/a"}</td><td>${typeof s.p === "number" ? s.p.toFixed(3) : "n/a"}</td><td>${s.early ? (s.kept ? "Yes" : "No") : "Not an early sign"}</td></tr>`).join("")}</tbody></table></div></div></div>`;
}
const money = (v) => (typeof v === "number" ? `${v < 0 ? "-" : ""}$${Math.round(Math.abs(v)).toLocaleString("en-US")}` : "n/a");
const eventBadge = (p) => badge(CALL_BADGE[p.action] || "quiet", p.call);
const checkNote = (p) => p.check ? `<p class="hold-note"><strong>${GATE.check_first[1]}</strong> ${esc(p.check)}</p>` : "";  // as Today's calls says it
const hostOptions = (ev, chosen = "") => list(ev.hosts).map((h) => `<option ${h.name === chosen ? "selected" : ""}>${esc(h.name)}</option>`).join("");
const profileLinks = (p) => list(p.links).length ? `<p class="small">${p.links.map((l) => source(l.url, l.label)).join(" · ")}</p>` : '<p class="small muted">No public profile on file.</p>';
function doorNote(ev, p) {
  return `<form class="door-note" data-event="${esc(ev.id)}" data-subject="${esc(p.subject_id)}"><div class="row"><strong>${esc(p.name)}</strong>${eventBadge(p)}</div>${p.note ? `<p class="small">Saved: ${esc(p.note)}</p>` : ""}<div class="door-fields"><select name="kind" aria-label="How the chat went"><option value="open">Open to talk now</option><option value="wait">Not now, check back on…</option><option value="context">Just a note</option></select><input type="date" name="wait_until" aria-label="Check back on (for not now)"><select name="author" aria-label="Who talked to them">${hostOptions(ev)}</select></div><textarea name="text" rows="2" maxlength="1000" required aria-label="What they said" placeholder="What they said, in their words"></textarea><div><button type="submit" class="button primary">Save</button></div></form>`;
}
function followUp(f, ev) {
  const state = f.sent ? badge("completed", `Followed up by ${f.sent.by} on ${date(f.sent.at)}`) : f.overdue ? badge("failed", "Overdue") : badge("pending", `Due ${stamp(f.due)}`);
  const ids = `data-event="${esc(ev.id)}" data-subject="${esc(f.subject_id)}"`;
  const body = f.sent ? "" : f.hold ? `<p class="hold-note"><strong>Nobody writes to them for now.</strong> ${esc(f.hold)}</p>` : `${f.invites_to ? `<p class="small">Its draft also invites them to ${esc(f.invites_to)}: marking it records both.</p>` : ""}<div class="draft"><div class="draft-subject">${esc(f.draft.subject)}</div><div class="draft-body">${esc(f.draft.body)}</div></div><div class="actions" style="margin-top:10px">${button("Copy draft", "copy-follow-up", "", ids)}${f.invites_to ? `${button("Mark as followed up and invited", "event-sent", "", `${ids} data-invite="${esc(f.invites_to_id)}"`)}${button("Sent without the invite", "event-sent", "", ids)}` : button("Mark as followed up", "event-sent", "", ids)}</div><p class="footnote">Press it once ${esc(f.host)} has sent it, so nobody else at GI messages them.</p>`;
  return `<div class="evidence-item"><div class="row"><strong>${esc(f.name)}</strong>${state}</div><p class="small">They said: ${esc(f.note)}</p>${body}</div>`;
}
function nextEvent(n) {
  const e = n.event, est = n.estimate;
  const person = (p) => {
    const ids = `data-event="${esc(e.id)}" data-subject="${esc(p.subject_id)}"`;
    const from = p.from ? `<p class="small"><strong>From ${esc(p.from)}:</strong> ${p.tie ? esc(p.tie) : "nobody at GI knows them yet, so the note claims no tie"}.</p>` : "";
    const hint = p.hint ? `<p class="small muted">${esc(p.hint)}.</p>` : "";
    const draft = p.draft ? `<div class="draft"><div class="draft-subject">${esc(p.draft.subject)}</div><div class="draft-body">${esc(p.draft.body)}</div></div>` : "";
    const both = p.follow_up ? `<p class="hold-note">Their follow-up from ${esc(p.follow_up)} is due, and its draft below now invites them too: send that one note, and mark it there.</p>` : "";
    const by = `<label class="small">Sent by <select name="by">${list(n.senders).map((s) => `<option ${s === p.from ? "selected" : ""}>${esc(s)}</option>`).join("")}</select></label>`;
    const act = p.follow_up ? "" : `<div class="actions" style="margin-top:10px">${p.draft ? button("Copy draft", "copy-invite", "", ids) : ""}${by}${button("Mark as invited", "event-invite", "", ids)}</div>`;
    return `<div class="evidence-item"><div class="row"><strong>${esc(p.name)}</strong>${eventBadge(p)}</div><p>${esc(p.why_them)}</p>${checkNote(p)}${profileLinks(p)}${from}${hint}${both}${draft}${act}</div>`;
  };
  const brief = (p, maybe) => `<div class="evidence-item"><div class="row"><strong>${esc(p.name)}</strong><span>${maybe ? badge("pending", "Suggested, not invited yet") + " " : ""}${eventBadge(p)}</span></div><p class="small">${esc(p.why_them)}</p>${p.tie ? `<p class="small"><strong>Your tie:</strong> ${esc(p.tie)}.</p>` : ""}${list(p.talk_about).length ? `<p class="small"><strong>Talk about:</strong> ${esc(p.talk_about.join(", "))}.</p>` : ""}<p class="small"><strong>Rule:</strong> ${esc(p.rule)}</p>${profileLinks(p)}</div>`;
  const byHost = Object.fromEntries(list(n.briefs).map((b) => [b.host, b]));
  const briefs = list(e.hosts).map((h) => {
    const b = byHost[h.name] || { people: [], if_invited: [] };
    return `<h3>${esc(h.name)}${list(h.topics).length ? ` <span class="small muted">· ${esc(h.topics.join(", "))}</span>` : ""}</h3>${list(b.people).length ? b.people.map((p) => brief(p, false)).join("") : '<p class="small muted">No watched person coming for them yet.</p>'}${list(b.if_invited).map((p) => brief(p, true)).join("")}`;
  }).join("");
  return `<div class="panel"><h2>Next: ${esc(e.name)}</h2><p class="small">${date(e.day)} · ${esc(e.city)} · ${esc(n.said_yes)} of ${esc(e.capacity)} seats taken · ${est ? `expected cost ${money(est.mid)} (${money(est.low)} to ${money(est.high)}), from ${esc(est.based_on.length)} past ${esc(e.format)}s` : `no past ${esc(e.format)} to estimate the cost from`}</p>
<h3>Who to invite</h3><p class="small">People for this night's roles who are in town, from the watchlist and from companies GI could hire from, strongest call first. Each says why meeting them matters now, and comes with a note from the GI person who knows them.</p>${n.blocked ? `<p class="hold-note">${esc(n.blocked)}</p>` : ""}${list(n.invite).length ? n.invite.map(person).join("") : '<p class="muted">Nobody else to add.</p>'}
${list(n.invited).length ? `<h3>Invited</h3><ul class="reason-list">${n.invited.map((i) => `<li>${esc(i.name)}, by ${esc(i.by)} on ${date(i.at)}</li>`).join("")}</ul>` : ""}
${list(n.held).length ? `<h3>Not inviting</h3><ul class="reason-list">${n.held.map((h) => `<li><strong>${esc(h.name)}</strong>: ${esc(h.reason)}</li>`).join("")}</ul>` : ""}
<p class="footnote">Nothing is sent from here. Each note is a draft to edit and send by hand, and names only a tie on file. Once it is sent, pick who sent it and press Mark as invited: every other team then holds off on that person until the evening.</p></div>
<div class="panel"><h2>Host briefs</h2><p class="small">Who each host should find on the night, why, what to talk about, and what not to do. People suggested above are listed under the host they fit, in case they are invited.</p>${briefs || '<p class="muted">No hosts on this event yet.</p>'}</div>`;
}
function watchlistInvites(w) {
  const person = (p) => `<div class="evidence-item"><div class="row"><strong>${esc(p.name)}</strong>${eventBadge(p)}</div><p class="small muted">${esc(p.role)}</p><p>${esc(p.why_them)}</p>${checkNote(p)}${profileLinks(p)}</div>`;
  return `<div class="panel"><h2>Who to invite to GI's next event</h2><p class="small">From your watchlist (${esc(w.people_file)}), strongest call first. Where they live is not on file, so check they can come.</p>${w.error ? `<p class="hold-note">${esc(w.error)}</p>` : ""}${list(w.invite).map(person).join("") || '<p class="muted">Nobody on the watchlist to invite.</p>'}${list(w.held).length ? `<h3>Not inviting</h3><ul class="reason-list">${w.held.map((h) => `<li><strong>${esc(h.name)}</strong>: ${esc(h.reason)}</li>`).join("")}</ul>` : ""}<p class="footnote">Nothing is sent from here. ${w.has_events ? "To plan seats and host briefs with these people, list them in research/private/events/people.json with their city and topics." : "To plan seats, cost and host briefs, add the event to research/private/events/events.json."}</p></div>`;
}
function eventsPage() {
  const data = ui.events;
  const head = heading("Events", "GI's own events as a way to meet people before everyone else does: who to invite and why, a brief for each host, what the night costs, and after it, how each chat went.", button("Refresh", "events-refresh"));
  if (!data) return `${head}<div class="content"><div class="panel"><p>${ui.eventsError ? esc(ui.eventsError) : "Running the engine…"}</p></div></div>`;
  if (data.missing) return `${head}<div class="content">${data.watchlist ? watchlistInvites(data.watchlist) : ""}<div class="panel"><h2>No event records yet</h2><p>${esc(data.missing)}</p><p class="footnote">Switch to Simulation to see the whole loop on invented records.</p></div></div>`;
  const l = data.last;
  const last = l ? `<div class="panel"><h2>After ${esc(l.event.name)} (${date(l.event.day)}): how did the chats go?</h2><p class="small">For each watched person who came, pick what they said. Your answer updates their call on Today's calls: "Open to talk now" gives the engine a reason to reach out, "Not now" holds everyone at GI until the day they gave, and "Just a note" is kept for whoever writes to them next.</p>${list(l.people).map((p) => doorNote(l.event, p)).join("") || '<p class="muted">No watched person checked in.</p>'}<h3>Follow-ups to send</h3><p class="small">Everyone who was open, or left a note, gets a draft from the host who talked to them, due two days after the event.</p>${list(l.follow_ups).map((f) => followUp(f, l.event)).join("") || '<p class="muted">None yet: save how a chat went above and its follow-up appears here.</p>'}</div>` : "";
  const row = (r) => `<tr><td>${esc(r.name)}<br><span class="small muted">${date(r.day)} · ${esc(r.format)}</span></td><td>${money(r.cost)}</td><td>${esc(r.came)} of ${esc(r.said_yes)}</td><td>${money(r.per_head)}</td><td>${esc(r.qualified)}</td><td>${money(r.cost_per_qualified)}</td><td>${esc(list(r.entered_pipeline).length)}</td></tr>`;
  const past = list(data.past).length ? `<div class="panel"><h2>Past events</h2><div class="table-scroll"><table><thead><tr><th>Event</th><th>Cost</th><th>Came</th><th>Per head</th><th>Qualified conversations</th><th>Cost per qualified</th><th>Into the pipeline within 90 days</th></tr></thead><tbody>${data.past.map(row).join("")}</tbody></table></div><p class="footnote">Cost is card spend tagged with the event in the Ramp export. A qualified conversation is a chat saved as "Open to talk now" or "Not now, check back on…" after the event.${ui.mode === "simulation" ? " Every event, guest, amount and host here is invented." : ""}</p></div>` : "";
  const budget = data.budget ? `<div class="panel"><h2>Event budget</h2><p>${budgetLine(data.budget)}</p></div>` : "";
  return `${head}<div class="content"><p class="small muted">${esc(data.source)}.</p>${data.people_error && !data.watchlist?.error ? `<p class="connection-error">${esc(data.people_error)}</p>` : ""}${data.watchlist ? watchlistInvites(data.watchlist) : ""}${data.next ? nextEvent(data.next) : ""}${budget}${last}${past}</div>`;
}
function budgetLine(b) {
  const later = list(b.planned).map((p) => (typeof p.to_come === "number" ? `, ${money(p.to_come)} still to come for ${esc(p.name)}` : `, no estimate yet for ${esc(p.name)}`)).join("");
  return `${esc(b.period)}: ${money(b.spent)} of ${money(b.amount)} spent${later}; ${b.left >= 0 ? `${money(b.left)} left` : `<strong>${money(-b.left)} over</strong>`}.`;
}
function startsPage() {
  const data = ui.starts;
  const head = heading("New starts", "From an accepted offer to a first day: what must be ready, who owns it and when it is due. Visa and relocation are only ever what the candidate says at the offer stage.", button("Refresh", "starts-refresh"));
  if (!data) return `${head}<div class="content"><div class="panel"><p>${ui.startsError ? esc(ui.startsError) : "Loading…"}</p></div></div>`;
  if (data.problem) return `${head}<div class="content"><div class="panel"><p class="connection-error">${esc(data.problem)}</p></div></div>`;
  const titles = Object.fromEntries(list(data.roles).map((r) => [r.id, r.title]));
  const owners = (chosen) => `<option value="">No owner yet</option>${list(data.owners).map((o) => `<option ${o === chosen ? "selected" : ""}>${esc(o)}</option>`).join("")}`;
  const states = (i) => Object.entries(data.states || {}).filter(([k]) => i.asked || !["ask", "not_needed"].includes(k)).map(([k, v]) => `<option value="${esc(k)}" ${k === i.state ? "selected" : ""}>${esc(v)}</option>`).join("");
  // Every change saves at once: no Save buttons. A finished item is one line, opened only to change it.
  const fields = (h, i) => `<form class="start-item door-fields" data-hire="${esc(h.id)}" data-key="${esc(i.key)}"><select name="owner" aria-label="Owner">${owners(i.owner)}</select><select name="state" aria-label="Status">${states(i)}</select>${i.asked ? `<input name="note" maxlength="300" value="${esc(i.note)}" placeholder="What the candidate said at the offer stage" aria-label="What the candidate said at the offer stage">` : ""}</form>`;
  const row = (h, i) => {
    const done = i.state === "done" || i.state === "not_needed";
    const status = done ? badge("completed", data.states[i.state]) : i.overdue ? badge("failed", `Overdue since ${date(i.due)}`) : badge("pending", `By ${date(i.due)}`);
    const line = `<strong>${esc(i.label)}</strong> <span class="small muted">${esc(i.owner || "No owner yet")}${i.note ? ` · ${esc(i.note)}` : ""}</span>`;
    return done ? `<details class="evidence-item"><summary class="row"><span>${line}</span>${status}</summary>${fields(h, i)}</details>` : `<div class="evidence-item"><div class="row"><span>${line}</span>${status}</div>${fields(h, i)}</div>`;
  };
  const hire = (h) => `<div class="panel"><div class="row"><h2>${esc(h.name)}</h2>${h.open ? badge("pending", `${h.open} open`) : badge("completed", "Ready")}</div><p class="small">${esc(titles[h.role_id] || h.role_id)} · ${esc(h.office)} · starts ${date(h.start)} · offer accepted ${date(h.offer_on)}</p>${h.items.map((i) => row(h, i)).join("")}<div class="actions" style="margin-top:10px">${button("Remove", "start-remove", "", `data-hire="${esc(h.id)}" data-name="${esc(h.name)}"`)}</div></div>`;
  const roleOptions = list(data.roles).map((r) => `<option value="${esc(r.id)}">${esc(r.title)}</option>`).join("");
  const plan = list(data.from_plan).length ? `<label class="small">From the hiring plan <select id="start-from-plan"><option value="">Someone else</option>${data.from_plan.map((p, n) => `<option value="${n}">${esc(p.name)}, ${esc(titles[p.role_id] || p.role_id)}</option>`).join("")}</select></label>` : "";
  const form = `<form class="start-add">${plan}<div class="door-fields"><input name="name" required minlength="2" maxlength="120" placeholder="Name" aria-label="Name"><select name="role_id" aria-label="Role">${roleOptions}</select><select name="office" aria-label="Office">${officeOptions(list(data.roles)[0]?.offices)}</select><label class="small">Starts <input type="date" name="start" required></label><label class="small">Offer accepted <input type="date" name="offer_on" value="${esc(data.as_of)}" max="${esc(data.as_of)}" required></label></div><div style="margin-top:10px"><button type="submit" class="button primary">Add</button></div><p class="footnote">Each item gets the ops team's default owner and a due day before the start. Visa and relocation start as "Ask at the offer".</p></form>`;
  const hires = list(data.hires);
  const add = hires.length ? `<details class="panel"><summary><strong>Add an accepted offer</strong></summary>${form}</details>` : `<div class="panel"><h2>Add an accepted offer</h2>${form}</div>`;
  return `${head}<div class="content">${data.note ? `<p class="small muted">${esc(data.note)}</p>` : ""}${hires.map(hire).join("") || '<div class="panel"><p class="muted">No accepted offers yet.</p></div>'}${add}</div>`;
}
function monthPage() {
  const data = ui.month;
  const head = heading("Monthly one-pager", "For the founders: the month so far on one page. Pipeline per role, spend against budget, and what's slipping.", `${button("Copy as text", "month-copy")} ${button("Print", "month-print")} ${button("Refresh", "month-refresh")}`);
  if (!data) return `${head}<div class="content"><div class="panel"><p>${ui.monthError ? esc(ui.monthError) : "Running the engine…"}</p></div></div>`;
  if (data.missing) return `${head}<div class="content"><div class="panel"><p>${esc(data.missing)}</p></div></div>`;
  const cell = (n) => `<td>${n ? esc(n) : '<span class="muted">0</span>'}</td>`;
  const rows = list(data.pipeline).map((r) => `<tr><td>${esc(r.title)}</td>${cell(r.to_reach)}${cell(r.reached)}${cell(r.replied)}${cell(r.active)}${cell(r.offers)}<td>${r.planned ? `${esc(r.filled)} of ${esc(r.planned)}` : '<span class="muted">none planned</span>'}</td>${cell(r.starting)}</tr>`).join("");
  const pipeline = `<div class="panel"><h2>Pipeline per role</h2>${rows ? `<div class="table-scroll"><table><thead><tr><th>Role</th><th>To reach</th><th>Reached</th><th>Replied</th><th>Active</th><th>Offers</th><th>${esc(data.year)} hires filled</th><th>Starting</th></tr></thead><tbody>${rows}</tbody></table></div><p class="footnote">${esc(data.footnote)}</p>` : '<p class="muted">Nobody in the pipeline yet.</p>'}</div>`;
  const b = data.people_budget;
  const people = b ? `<p><strong>People ${esc(b.year)}:</strong> ${money(b.total)} of ${money(b.budget)}, ${b.left >= 0 ? `${money(b.left)} left` : `<strong>${money(-b.left)} over</strong>`}, for payroll and ${esc(b.hires)} planned ${b.hires === 1 ? "hire" : "hires"}${b.uncosted ? `; ${esc(b.uncosted)} not costed yet` : ""}.</p>` : `<p><strong>People:</strong> ${data.plan_unreadable ? "the saved hiring plan could not be read; fix it on the Hiring budget page." : "no yearly budget set on the Hiring budget page."}</p>`;
  const spend = `<div class="panel"><h2>Spend against budget</h2>${people}${data.event_budget ? `<p><strong>Events</strong> ${budgetLine(data.event_budget)}</p>` : ""}</div>`;
  const slipping = `<div class="panel"><h2>What's slipping</h2>${list(data.slipping).length ? `<ul class="reason-list">${data.slipping.map((s) => `<li><strong>${esc(s.title)}</strong>${s.why ? `: ${esc(s.why)}` : ""}</li>`).join("")}</ul>` : '<p class="muted">Nothing overdue.</p>'}</div>`;
  return `${head}<div class="content"><p class="small muted">${esc(data.month)}, as of ${date(data.as_of)}. ${esc(data.source)}.</p><div class="panel"><p><strong>${esc(data.headline)}</strong></p></div>${pipeline}${spend}${slipping}<p class="footnote">From the Monday brief, the contact ledger, the Hiring budget and New starts. Like the brief, it never mentions a visa.${data.invented ? " Every person and number here is invented." : ""}</p></div>`;
}
async function saveStartItem(form) {
  const fd = new FormData(form);
  await api(`/api/starts/${encodeURIComponent(form.dataset.hire)}`, "PATCH", { mode: ui.mode, key: form.dataset.key, owner: String(fd.get("owner") || ""), state: String(fd.get("state")), ...(fd.has("note") ? { note: String(fd.get("note")).trim() } : {}) });
  ui.dirty = false;
  opsChanged(); // the Monday brief lists what falls due
  toast("Saved.");
  await refresh(true);
}
function weekPage() {
  const data = ui.week;
  const head = heading("Monday brief", "The few things worth your attention this week, most urgent first: who to reach, who to invite, and what it costs. The same list goes to GI's Slack as one card.", button("Refresh", "week-refresh"));
  if (!data) return `${head}<div class="content"><div class="panel"><p>${ui.weekError ? esc(ui.weekError) : "Running the engine…"}</p></div></div>`;
  if (data.missing) return `${head}<div class="content"><div class="panel"><h2>Nothing to judge yet</h2><p>${esc(data.missing)}</p><p class="footnote">Switch to Simulation to see the week on invented people.</p></div></div>`;
  const who = (p) => `<li><strong>${esc(p.name)}</strong>: ${esc(p.why)}${p.note ? ` <em>(${esc(p.note)})</em>` : ""}</li>`;
  const role = (r) => `<div class="panel"><h2>${esc(r.title)}</h2><p class="small">${esc(r.pay)}</p><h3>Reach this week</h3>${list(r.reach).length ? `<ul class="reason-list">${r.reach.map(who).join("")}</ul>` : '<p class="muted">Nobody to reach this week.</p>'}${r.more ? `<p class="small">…and ${esc(r.more)} more on Today's calls, past this week's cap of cards ${r.all_used ? "across all roles" : "for the role"}.</p>` : ""}${list(r.check_first).length ? `<h3>Check first</h3><ul class="reason-list">${r.check_first.map(who).join("")}</ul>` : ""}${list(r.held).length ? `<h3>Held by the contact ledger</h3><ul class="reason-list">${r.held.map((h) => `<li><strong>${esc(h.name)}</strong>: ${esc(h.reason)}</li>`).join("")}</ul>` : ""}</div>`;
  const e = data.event;
  const event = e ? `<div class="panel"><h2>Next event: ${esc(e.name)}, ${date(e.day)}</h2><p class="small">${esc(e.seats_left)} seats open${e.held ? ` · ${esc(e.held)} held back` : ""}</p>${list(e.invite).length ? `<ul class="reason-list">${e.invite.map((p) => `<li><strong>${esc(p.name)}</strong>: ${esc(p.call)}${p.asked ? " <em>(already in this week's brief: one ask at a time, so no separate invite)</em>" : ""}</li>`).join("")}</ul>` : e.blocked ? `<p class="hold-note">${esc(e.blocked)}</p>` : '<p class="muted">Nobody else to invite.</p>'}<p class="footnote">Invites are marked on the Events page once a host has sent them.</p></div>` : "";
  const ending = `<div class="panel"><h2>Waits ending this week</h2>${list(data.ending).length ? `<ul class="reason-list">${data.ending.map((x) => `<li><strong>${esc(x.name)}</strong> (${esc(x.role)}): ${esc(x.headline.toLowerCase())} until ${date(x.until)}</li>`).join("")}</ul>` : '<p class="muted">No wait ends this week.</p>'}</div>`;
  const due = `<div class="panel"><h2>Follow-ups due</h2>${list(data.follow_ups).length ? `<ul class="reason-list">${data.follow_ups.map((f) => `<li><strong>${esc(f.name)}</strong>${f.from ? `, from ${esc(f.from)}` : ""}${f.reason ? `: ${esc(f.reason)}` : ""}${f.overdue ? " (overdue)" : f.due ? ` (due ${stamp(f.due)})` : ""}</li>`).join("")}</ul>` : '<p class="muted">None due. A door note logged on the Events page adds one.</p>'}</div>`;
  const budget = data.budget ? `<div class="panel"><h2>Event budget</h2><p>${budgetLine(data.budget)}</p><p class="footnote">Spend is card spend tagged with an event in the Ramp export; expected is the estimate from past events of the same kind.${ui.mode === "simulation" ? " The budget and every amount here are invented." : ""}</p></div>` : "";
  const card = `<p class="footnote">The same list goes to GI's Slack as one card on Monday morning. It contacts no one, and stays silent in a week with nothing to decide.${data.preview ? ` <a href="${esc(url(data.preview))}" target="_blank" rel="noopener noreferrer">See the card ↗</a>` : ""}</p>`;
  const when = (d) => d.overdue ? badge("failed", "Overdue") : d.due ? badge("pending", `By ${date(d.due)}`) : "";
  const act = (d) => [d.cost ? esc(d.cost) : "", d.act ? `<a href="${esc(url(d.act.url))}" target="_blank" rel="noopener noreferrer">${esc(d.act.label)} ↗</a>` : `On ${esc(d.where)}`].filter(Boolean).join(" · ");
  const item = (d, n) => `<div class="evidence-item"><div class="row"><strong>${n + 1}. ${esc(d.title)}</strong><span>${when(d)}</span></div><p class="small muted">${esc(d.area)}</p><p>${esc(d.why)}</p>${d.way_in ? `<p class="small"><strong>Way in:</strong> ${esc(d.way_in)}</p>` : ""}<p class="small">${act(d)}</p></div>`;
  const items = list(data.decisions);
  const brief = `<div class="panel"><h2>${items.length ? `${items.length} ${items.length === 1 ? "thing" : "things"} worth your attention` : "Nothing to decide this week"}</h2>${items.map(item).join("") || '<p class="muted">The brief stays silent: no card goes to Slack.</p>'}${card}</div>`;
  return `${head}<div class="content"><p class="small muted">${esc(data.source)}. Week of ${date(data.week_of)}, as of ${date(data.as_of)}.</p>${freshLine(data.fresh)}${brief}<details class="week-detail"><summary>The week in detail: per role, waits, the next event, follow-ups and event spend</summary>${list(data.roles).map(role).join("")}${ending}${event}${due}${budget}</details></div>`;
}
const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];
const monthOptions = (chosen = 1) => MONTHS.map((m, i) => `<option value="${i + 1}" ${i + 1 === chosen ? "selected" : ""}>${m}</option>`).join("");
const sourceOptions = (sources, chosen = "outreach") => Object.entries(sources || {}).map(([k, v]) => `<option value="${esc(k)}" ${k === chosen ? "selected" : ""}>${esc(v)}</option>`).join("");
const planLines = (data) => list(data?.projection?.lines).map(({ role_id, subject_id, name, office, count, start_month, source }) => ({ role_id, subject_id, name, office, count, start_month, source }));
const officeOptions = (offices) => list(offices).map((o) => `<option value="${esc(o)}">${esc(o)}</option>`).join("");
const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
function hiringAssumptions(form) {
  const fd = new FormData(form), num = (k) => Number(fd.get(k));
  const estimates = Object.fromEntries(list(ui.hiring?.roles).filter((r) => !r.posted && String(fd.get(`est-${r.id}`) || "").trim() !== "").map((r) => [r.id, num(`est-${r.id}`)]));
  return { ...ui.hiring.assumptions, year: num("year"), yearly_budget: num("yearly_budget"), overhead_pct: num("overhead_pct"), outreach_cost: num("outreach_cost"), agency_fee_pct: num("agency_fee_pct"), conversations_per_hire: num("conversations_per_hire"), salary_estimates: estimates };
}
function hiringPage() {
  const data = ui.hiring;
  const head = heading("Hiring budget", "What the people the engine surfaces would cost GI if hired, and how the plan and current payroll fit the year's budget. Every figure is the role's cost to GI, never a guess at what anyone earns now.", button("Refresh", "hiring-refresh"));
  if (!data) return `${head}<div class="content"><div class="panel"><p>${ui.hiringError ? esc(ui.hiringError) : "Running the engine…"}</p></div></div>`;
  const a = data.assumptions, pr = data.projection, roles = Object.fromEntries(list(data.roles).map((r) => [r.id, r]));
  const pct = pr.budget ? Math.min(100, (pr.total / pr.budget) * 100) : pr.total ? 100 : 0;
  const uncosted = list(pr.lines).filter((l) => l.needs);
  const leftOut = uncosted.length ? ` Not counting ${plural(pr.uncosted_hires, "hire", "hires")} that can't be costed yet.` : "";
  const why = [...new Set(uncosted.map((l) => l.needs))].map((n) => n === "a salary" ? "give the role a salary estimate below" : "find the hire another way than a GI event (no past event has a qualified conversation to cost it from)");
  const pay = data.payroll;
  const baseline = pay && !pay.error ? `<p class="small">Current payroll ${money(pr.payroll)} (${plural(pay.people, "person", "people")} from the payroll export, a full year plus ${esc(a.overhead_pct)}% overhead) and new hires ${money(pr.hires_cost)}.${pay.unpaid ? ` ${plural(pay.unpaid, "row", "rows")} with no salary left out.` : ""}</p>${list(pay.unmatched).length ? `<p class="hold-note">Payroll offices that match no job post's location: ${esc(pay.unmatched.join(", "))}. Rename them in the export to count their desks with the right office.</p>` : ""}` : `<p class="hold-note">${pay ? esc(pay.error) : `No payroll export on file, so this counts new hires only. Export a CSV from the payroll tool with team, office and annual_salary columns (no names needed) to ${esc(data.payroll_path)}.`}</p>`;
  const summary = `<div class="panel"><h2>${esc(a.year)} people costs: ${money(pr.total)} of ${money(pr.budget)}</h2>${baseline}<div class="budget-bar ${pr.over ? "over" : ""}" role="img" aria-label="${Math.round(pct)}% of the budget"><span style="width:${pct}%"></span></div><p>${pr.over ? `<strong class="over-text">Over budget by ${money(-pr.left)}.</strong> Push a start month back, drop a line, or raise the budget.` : `<strong>${money(pr.left)} left.</strong>`}${leftOut} ${plural(pr.hires, "hire", "hires")} planned; once ${pr.unsalaried_hires ? "all with a salary are" : "all are"} on board they cost ${money(pr.run_rate)} a year.</p>${list(pr.by_role).length ? `<ul class="reason-list">${pr.by_role.map((r) => `<li>${esc(r.title)}: ${plural(r.hires, "hire", "hires")}, ${money(r.cost)} in ${esc(a.year)}${r.uncosted ? ` (${esc(r.uncosted)} not costed yet)` : ""}</li>`).join("")}</ul>` : ""}${uncosted.length ? `<p class="hold-note">${uncosted.length === 1 ? "One line" : `${uncosted.length} lines`} can't be costed yet, so the totals leave them out: ${esc(why.join("; or "))}.</p>` : ""}</div>`;
  const row = (l) => `<tr><td>${l.subject_id ? esc(l.name || l.subject_id) : `${esc(l.count)} open ${l.count === 1 ? "seat" : "seats"}`}</td><td>${esc(l.role_title)}</td><td>${esc(l.office)}</td><td>${MONTHS[l.start_month - 1]} (${esc(l.months)} months)</td><td>${esc(l.source_label)}</td><td>${typeof l.cost === "number" ? money(l.cost) : `<span class="muted">needs ${esc(l.needs)}</span>`}</td><td>${button("Remove", "hiring-remove", "", `data-index="${esc(l.index)}"`)}</td></tr>`;
  const plan = `<div class="panel"><h2>The plan</h2>${list(pr.lines).length ? `<div class="table-scroll"><table><thead><tr><th>Hire</th><th>Role</th><th>Office</th><th>Starts</th><th>Found through</th><th>Cost in ${esc(a.year)}</th><th></th></tr></thead><tbody>${pr.lines.map(row).join("")}</tbody><tfoot><tr><td colspan="5"><strong>New hires</strong></td><td><strong>${money(pr.hires_cost)}</strong></td><td></td></tr></tfoot></table></div>` : '<p class="muted">Nothing planned yet. Add a surfaced person or open seats below.</p>'}<p class="footnote">Cost in the year: salary from the start month to December, plus ${esc(a.overhead_pct)}% overhead, plus the one-time cost of finding the hire.</p></div>`;
  const firstYear = (p) => { const r = roles[p.role_id]; return r && typeof r.first_year === "number" ? `About <strong>${money(r.first_year)}</strong> in a first year if hired through GI's own outreach: ${money(r.salary)} salary (${esc(r.basis)}) plus ${esc(a.overhead_pct)}% overhead${a.outreach_cost ? `, plus ${money(a.outreach_cost)} to find them` : ""}.` : `No cost yet: ${esc(r ? r.basis : "the role is not in config/roles.json")}.`; };
  const person = (p) => `<div class="evidence-item hiring-row"><div class="row"><strong>${esc(p.name)}</strong><span>${badge(CALL_BADGE[p.action] || "quiet", p.headline)}${p.gate ? " " + badge("suppressed", GATE[p.gate][0]) : ""}</span></div><p class="small muted">${esc(p.role_title)} · ${esc(p.why)}</p><p class="small">${firstYear(p)}</p>${p.planned ? '<p class="small"><strong>In the plan.</strong></p>' : !roles[p.role_id] ? '<p class="small muted">Add their role to config/roles.json to plan this hire.</p>' : `<div class="door-fields"><label class="small">Office <select name="office" aria-label="Office">${officeOptions(roles[p.role_id].offices)}</select></label><label class="small">Starts <select name="start" aria-label="Start month">${monthOptions(1)}</select></label><label class="small">Found through <select name="source" aria-label="Found through">${sourceOptions(data.sources)}</select></label>${button("Add to plan", "hiring-add-person", "", `data-subject="${esc(p.subject_id)}"`)}</div>`}</div>`;
  const people = `<div class="panel"><h2>Surfaced by the engine</h2><p class="small">The people Today's calls says to reach now or check first, with what hiring each would cost GI in a first year.</p>${list(data.people).map(person).join("") || `<p class="muted">${data.missing ? esc(data.missing) : "Nobody surfaced right now."}</p>`}</div>`;
  const seat = (r) => `<div class="evidence-item hiring-row"><div class="row"><strong>${esc(r.title)}</strong><span class="small">${typeof r.first_year === "number" ? `${money(r.first_year)} a first year` : "needs a salary"}</span></div><p class="small muted">${esc(r.pay)}</p><div class="door-fields"><label class="small">How many <input type="number" name="count" min="1" max="50" value="1" aria-label="How many"></label><label class="small">Office <select name="office" aria-label="Office">${officeOptions(r.offices)}</select></label><label class="small">Starts <select name="start" aria-label="Start month">${monthOptions(1)}</select></label><label class="small">Found through <select name="source" aria-label="Found through">${sourceOptions(data.sources)}</select></label>${button("Add open seats", "hiring-add-seats", "", `data-role="${esc(r.id)}"`)}</div></div>`;
  const offices = list(pr.offices).length ? `<div class="panel"><h2>Desks by office</h2><div class="table-scroll"><table><thead><tr><th>Office</th><th>On payroll now</th><th>Planned hires</th><th>Desks by December</th></tr></thead><tbody>${pr.offices.map((o) => `<tr><td>${esc(o.office)}</td><td>${esc(o.now)}</td><td>${esc(o.planned)}</td><td>${esc(o.desks)}</td></tr>`).join("")}</tbody></table></div><p class="footnote">A hire's office is one of the locations in its role's job post.</p></div>` : "";
  const seats = `<div class="panel"><h2>Open seats</h2><p class="small">Hires not tied to anyone the engine has surfaced yet.</p>${list(data.roles).map(seat).join("")}</div>`;
  const ec = data.event_cost;
  const salaryField = (r) => r.posted ? `<p class="small"><strong>${esc(r.title)}:</strong> ${esc(r.pay)}. The plan uses the midpoint.</p>` : `<label class="small"><strong>${esc(r.title)}</strong><span>${data.invented ? "Salary estimate, $ a year (invented for the demo)" : "Your salary estimate, $ a year"}. ${esc(r.pay)}.</span><input type="number" name="est-${esc(r.id)}" min="1" max="10000000" step="any" value="${typeof r.estimate === "number" ? esc(r.estimate) : ""}" placeholder="e.g. 200000"></label>`;
  const assumptions = `<form class="panel hiring-form"><h2>Assumptions</h2>${a.note ? `<p class="small muted">${esc(a.note)}</p>` : ""}<div class="door-fields"><label class="small">Year <input type="number" name="year" min="${esc(data.min_year || 2020)}" max="2100" value="${esc(a.year)}" required></label><label class="small">Yearly people budget ($): current payroll plus new hires <input type="number" name="yearly_budget" min="0" max="1000000000" step="any" value="${esc(a.yearly_budget)}" required></label><label class="small">Overhead: benefits and payroll taxes (% of salary) <input type="number" name="overhead_pct" min="0" max="200" step="any" value="${esc(a.overhead_pct)}" required></label></div><h3>Cost of finding a hire</h3><div class="door-fields"><label class="small">GI's own outreach ($ per hire) <input type="number" name="outreach_cost" min="0" max="10000000" step="any" value="${esc(a.outreach_cost)}" required></label><label class="small">Agency fee (% of first-year salary) <input type="number" name="agency_fee_pct" min="0" max="100" step="any" value="${esc(a.agency_fee_pct)}" required></label><label class="small">Qualified event conversations per hire <input type="number" name="conversations_per_hire" min="0" max="100" step="any" value="${esc(a.conversations_per_hire)}" required></label></div><p class="small">${ec ? `A GI event: ${money(ec.per_qualified)} of event spend per qualified conversation (${esc(ec.qualified)} across ${esc(ec.events)} past events, from the Events page), so ${money(ec.per_qualified * a.conversations_per_hire)} per hire.` : "A GI event: no past event has a qualified conversation yet, so an event hire can't be costed."}</p><h3>Salaries</h3>${list(data.roles).map(salaryField).join("")}<p class="footnote">Posted ranges come from config/pay-bands.json, which scripts/pay_bands.py reads from GI's job posts. The pay line on Slack cards reads the same file, so the two never disagree.</p><div style="margin-top:10px"><button type="submit" class="button primary">Update</button></div></form>`;
  return `${head}<div class="content"><p class="small muted">${esc(data.source || "No timelines yet")}.${data.invented ? " Every amount on this page is invented for the demo." : " Saved to research/private/hiring/plan.json."}</p>${data.problem ? `<p class="hold-note">${esc(data.problem)}</p>` : ""}${data.ledger_problem ? `<p class="hold-note">${esc(data.ledger_problem)}</p>` : ""}${summary}${plan}${offices}${people}${seats}${assumptions}</div>`;
}
async function savePlan(assumptions, lines) {
  // One save at a time: each builds on the plan the last one returned. Every hiring button waits for it.
  const mode = ui.mode, buttons = [...document.querySelectorAll(".hiring-row button, .hiring-form button, [data-action=hiring-remove]")];
  ui.hiringSaving = true;
  buttons.forEach((b) => { b.disabled = true; });
  try {
    const page = await api("/api/hiring", "PUT", { mode, assumptions, lines });
    if (ui.mode === mode) { // a save that returns after a workspace switch never shows there
      ui.hiring = page;
      opsChanged(); // the brief and the one-pager judge the plan
    }
    ui.hiringSaving = false;
    render();
  } finally {
    ui.hiringSaving = false;
    buttons.forEach((b) => { b.disabled = false; }); // on a refused save the form keeps what was typed
  }
}
function reviewPage() {
  const candidates = getCandidates();
  let c = candidates.find((x) => x.id === ui.selected);
  if (!c && ui.selected) ui.selected = null;
  if (!c && candidates.length && window.innerWidth > 620) {
    c = candidates[0];
    ui.selected = c.id;
  }
  return `${heading(ui.view === "inbox" ? "Worth a conversation now" : "People worth knowing", ui.view === "inbox" ? "Review the reason, the evidence, and the approach." : "Strong possibilities, missing evidence, and moments still developing.", `${button("Add person", "add")}${button("Evaluate signals", "evaluate-run", "primary")}`)}<div class="toolbar"><input id="candidate-search" type="search" aria-label="Search candidates" placeholder="Search people or companies" value="${esc(ui.query)}"><select id="role-filter" aria-label="Filter by role"><option value="all">All roles</option>${list(
    ui.state.roles,
  )
    .map(
      (r) =>
        `<option value="${esc(r.id)}" ${ui.role === r.id ? "selected" : ""}>${esc(r.title)}</option>`,
    )
    .join(
      "",
    )}</select>${button("Find candidates", "discover")}</div><div class="review-grid ${c ? "has-selection" : ""}"><section class="queue" aria-label="Candidate queue"><div class="queue-heading"><span>${candidates.length} ${candidates.length === 1 ? "person" : "people"}</span><span>${ui.view === "inbox" ? "Ready or awaiting review" : "All decision states"}</span></div>${
    candidates.length
      ? candidates
          .sort(
            (a, b) =>
              priority(b.decision?.priority) - priority(a.decision?.priority),
          )
          .map(
            (x) =>
              `<button class="candidate ${x.id === ui.selected ? "selected" : ""}" data-candidate="${esc(x.id)}" aria-pressed="${x.id === ui.selected}"><div class="candidate-name">${esc(x.name)}</div><div class="candidate-role">${esc(roleTitle(x.role_id))}<br>${esc(x.employer || "Employer not verified")}</div><div class="row">${badge(x.decision?.state)}<span class="small muted">${date(eventFor(x)?.date)}</span></div><div class="candidate-excerpt">${esc(eventFor(x)?.why_now || x.decision?.reasons?.[0] || x.summary)}</div></button>`,
          )
          .join("")
      : `<div class="empty"><h2>${ui.view === "inbox" ? "No pings to review" : "Start with a person"}</h2><p>${ui.view === "inbox" ? "Evaluate the watchlist to surface supported opportunities." : "Discover candidates or add a professional profile and evidence."}</p>${button(ui.view === "inbox" ? "Open watchlist" : "Add person", ui.view === "inbox" ? "watchlist" : "add")}</div>`
  }</section><article class="detail">${c ? candidateDetail(c) : `<div class="empty"><h2>A good reason comes first.</h2><p>Choose a person to inspect the evidence and timing decision.</p></div>`}</article></div>`;
}
function candidateDetail(c) {
  const d = c.decision || {},
    e = eventFor(c),
    r = c.route || {},
    draft = c.draft || {};
  const roleCriteria = list(ui.state?.roles?.find((role) => role.id === c.role_id)?.criteria);
  const supportedCriteria = new Set(list(c.fit)
    .filter((fit) => fit.status === "supported" && list(fit.evidence_ids).some((id) => list(c.evidence).some((source) => source.id === id)))
    .map((fit) => fit.criterion));
  const fitSupported = d.fit_supported ?? roleCriteria.filter((criterion) => supportedCriteria.has(criterion)).length;
  const fitTotal = d.fit_total ?? roleCriteria.length;
  const assessmentComplete = ["rapport", "hiring"].includes(e?.contact_purpose) && e?.purpose_reason?.statement && e?.purpose_reason?.evidence_id && e?.purpose_reason?.quote && e?.timing_assessment && !["context_only", "unknown"].includes(e.timing_assessment.mechanism) && ["person_impact", "opportunity", "waiting_cost"].every((key) => {
    const claim = e.timing_assessment[key];
    return claim?.statement && claim?.evidence_id && claim?.quote;
  });
  const readinessMeta = d.readiness ? `${d.readiness.window ? `<br>Measured window (${esc(human(d.readiness.window.detector_id))}): ${day(d.readiness.window.opens)} to ${day(d.readiness.window.closes)}` : ""}<br>Readiness: ${esc(human(d.readiness.action))} (${esc(d.readiness.confidence)} confidence)` : "";
  // Readiness decides timing whenever the person has a timeline; the model's own call stands alone only without one.
  // Under reach now, a hold on the hook itself still shows: a request to wait on the record, a contradicted trigger.
  const timingLabel = ["suppressed", "snoozed"].includes(d.state) ? "Do not contact now — see the hold below" : d.readiness?.action === "reach_now" && d.state === "watch" && e?.timing_action === "follow_up" ? "Use the source-backed follow-up date below" : d.readiness?.action === "reach_now" && e?.timing_action === "verify_first" ? "Verify first: the trigger was contradicted or superseded" : d.readiness ? { reach_now: "Now: the timeline says reach out", verify_first: "Verify first: check the named fact before reaching out", watch_until: `Wait until ${day(d.readiness.until)}`, respect_follow_up: `Wait until ${day(d.readiness.until)}`, quiet: "Not now — keep watching" }[d.readiness.action] : e?.timing_action === "contact_now" && !assessmentComplete ? "No justified outreach time — timing reassessment required" : e?.timing_action === "contact_now" && d.state === "watch" ? "No outreach time established — keep watching" : ({ contact_now: "Now, provisionally — evidence and sender need review", watch: "No outreach time established — keep watching", follow_up: "Use the source-backed follow-up date below", verify_first: "Verify first: the trigger was contradicted or superseded" }[e?.timing_action] || "No outreach time established");
  return `${button("‹ Back to people", "back", "mobile-back")}<div class="person-heading"><div><h2>${esc(c.name)}</h2><p>${esc(c.employer || "Employer not verified")} ${c.location ? " / " + esc(c.location) : ""}</p><p>${esc(roleTitle(c.role_id))}</p></div><div class="actions">${profileLink(c)}${button("Watch this person", "watch-enroll")}${button("Edit", "edit")}</div></div>${list(c.possible_aliases).length ? `<div class="callout warning" style="margin-bottom:18px">Another profile has the same name. Check whether these records refer to the same person before outreach. ${button("Compare profiles", "aliases")}</div>` : ""}<p class="detail-summary">${esc(c.summary || "Add a research summary grounded in professional evidence.")}</p><div class="timing"><div class="row">${badge(d.state)}<span class="small muted">${e ? "Timing hypothesis" : "Evidence check"}</span></div><h3>${esc(e?.title || "No verified timing event yet")}</h3>${e ? contactPurpose(e) : ""}<p>${esc(e?.why_now || "Research must establish a specific, current reason for a conversation.")}</p>${!e && d.readiness ? `<div class="timing-meta">Suggested outreach timing: ${esc(timingLabel)}${readinessMeta}</div>` : ""}${e ? `<div class="timing-meta">${e.date_basis === "source_statement_date" ? "Source statement date" : e.date_basis === "observed_change_date" ? "Change observed" : "Professional development"}: ${date(e.date)}<br>Suggested outreach timing: ${esc(timingLabel)}${readinessMeta}</div><p class="footnote">An event means a dated professional development, such as a release or job change. It does not imply conference attendance or willingness to change jobs.</p>${e.recipient_value ? `<p><strong>Value to this person:</strong> ${esc(e.recipient_value)}</p>` : ""}${e.timing_action === "follow_up" ? `<p><strong>Requested or constrained follow-up:</strong> ${date(e.follow_up_on)}</p>${e.follow_up_quote ? `<blockquote>${esc(e.follow_up_quote)}</blockquote>` : `<p class="footnote">The explicit source-backed reason for this date still needs verification.</p>`}` : ""}${e.date_basis === "observed_change_date" ? `<p class="footnote">Observation date: ${esc(e.date_caveat || "The underlying event date is not established.")}</p>` : e.date_basis === "source_statement_date" ? `<p class="footnote">Statement date, not a verified career or project milestone: ${esc(e.date_caveat || "The source reports this statement date; the underlying milestone date is not established.")}</p>` : e.date_caveat ? `<p class="footnote">${esc(e.date_caveat)}</p>` : ""}${list(e.evidence_ids).length ? `<p class="footnote">Trigger evidence: ${e.evidence_ids.map((id) => `<a href="#evidence-${esc(id)}">${esc(id)}</a>`).join(", ")}</p>` : ""}${e.why_wait ? `<p><strong>Why wait:</strong> ${esc(e.why_wait)}</p>` : ""}${e.falsifier ? `<p style="margin-top:8px"><strong>What could prove this wrong:</strong> ${esc(e.falsifier)}</p>` : ""}<div class="footnote">Confidence: ${esc(e.confidence || "Not assessed")}. This is an assessment, not a response probability.</div>` : ""}${e ? timingAssessment(e) : ""}${list(d.reasons).length ? `<ul class="reason-list">${d.reasons.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : ""}${d.next_check_at ? `<div class="footnote">${ui.state?.settings?.monitoring_enabled ? "Next" : "Suggested"} source recheck: ${stamp(d.next_check_at)}. ${ui.state?.settings?.monitoring_enabled ? "" : "Monitoring is off. "}This is a research schedule, not a predicted outreach time.</div>` : ""}</div><div class="metrics"><div class="metric"><div class="metric-label">Evidence coverage</div><div class="metric-value">${fitSupported} / ${fitTotal}</div><div class="metric-caption">Requirements with cited support. Assessment is unverified until review; this is not a suitability probability.</div></div><div class="metric"><div class="metric-label">Conversation value</div><div class="metric-value">${score(d.conversation_score ?? e?.conversation_score)}${typeof (d.conversation_score ?? e?.conversation_score) === "number" ? " / 3" : ""}</div><div class="metric-caption">Worthwhile exchange now</div></div><div class="metric"><div class="metric-label">Career openness</div><div class="metric-value">${score(d.career_openness ?? e?.career_openness)}</div><div class="metric-caption">Separate from conversation value</div></div></div><section class="section"><div class="section-title"><h3>Evidence of fit</h3>${badge(c.identity_status, "Identity " + human(c.identity_status))}</div>${list(c.fit).length ? c.fit.map((f) => `<div class="criterion">${badge(f.status)}<div>${esc(f.criterion)}${list(f.evidence_ids).length ? `<div class="footnote" style="margin-top:2px">Sources: ${f.evidence_ids.map((x) => `<a href="#evidence-${esc(x)}">${esc(x)}</a>`).join(", ")}</div>` : ""}</div></div>`).join("") : '<p class="small muted">No requirements have been assessed yet.</p>'}</section><section class="section"><div class="section-title"><h3>Source evidence</h3><div class="actions"><span class="small muted">${list(c.evidence).length} ${list(c.evidence).length === 1 ? "source" : "sources"}</span>${button("Add evidence", "add-evidence")}</div></div>${list(c.evidence).length ? c.evidence.map((ev) => `<div class="evidence-item" id="evidence-${esc(ev.id)}"><div class="row"><a class="evidence-title" href="${esc(url(ev.url))}" target="_blank" rel="noopener noreferrer">${esc(ev.title || ev.id)} ↗</a>${badge(ev.verified ? "verified" : "unknown", ev.verified ? "Verified" : "Needs verification")}</div>${sourceExcerpt(ev)}<div class="evidence-meta"><span>${esc(human(ev.source_kind))}</span><span>Professional development: ${date(ev.event_date)}</span><span>Observed: ${stamp(ev.observed_at)}</span></div></div>`).join("") : '<p class="small muted">Add clickable sources before confirming this candidate.</p>'}</section><section class="section"><div class="section-title"><h3>Route into the conversation</h3><div class="actions">${badge(r.status || "none_found")}${button("Edit route", "edit-route")}</div></div><p class="route-text">${esc(r.description || "No warm path found.")}</p>${r.introducer ? `<p class="footnote">Potential introducer: ${esc(r.introducer)}</p>` : ""}${r.status === "plausible_unconfirmed" ? '<div class="footnote">A possible relationship needs confirmation before requesting an introduction.</div>' : ""}</section>${privateNotes(c)}<section class="section"><div class="section-title"><h3>Draft outreach</h3><div class="actions">${button("Edit draft", "edit-draft")}${button("Copy draft", "copy")}</div></div><div class="draft"><div class="draft-meta"><span>From: ${esc(draft.sender_name || draft.sender_function || "Sender not assigned")}</span><span>${esc(human(draft.channel || "email"))}</span></div><div class="draft-subject">${esc(draft.subject || "No subject drafted")}</div><div class="draft-body">${esc(draft.body || "Research and verify the opportunity to prepare a specific message.")}</div></div><p class="footnote">Prepared for human review. Confirm the senior sender and the evidence before using this draft.</p></section><div class="callout">${d.state === "ready" ? "This opportunity has passed the current checks. Review the actual source and draft before contacting the person." : "A complete ping requires supported fit, verified evidence, a current reason, and a usable draft."}</div><div class="decision-footer">${button("Verify & review", "review", "primary")}${button("Research updates", "research")}${button("Snooze", "snooze")}${button("Log outreach", "mark_sent")}${button("Give feedback", "feedback", "tertiary")}${button("More actions", "more", "tertiary")}</div>`;
}
function privateNotes(c) {
  const notes = list(c.private_notes);
  return `<section class="section"><div class="section-title"><h3>Private notes</h3><span class="small muted">Tier 0 · internal only</span></div><p class="footnote">Context no public source shows: a referral, a past conversation, a stated preference. Each note is kept with its author and date. It counts for timing, at the highest trust tier, only when marked: open to a move, or not until a day. Edit fixes wording; a new conversation is a new note.</p>${notes.map((n) => `<div class="evidence-item"><div class="draft-body">${esc(n.quote)}</div><div class="evidence-meta"><span>${esc(n.author)}</span><span>${stamp(n.observed_at)}</span>${n.event_type === "note_open" ? "<span>Open to a move</span>" : n.event_type === "note_wait" ? `<span>Not until ${date(n.event_date)}</span>` : ""}${button("Edit", "edit-note", "tertiary", `data-note-id="${esc(n.id)}"`)}</div></div>`).join("")}<form id="private-note-form" data-person-id="${esc(c.person_id)}"><input type="hidden" name="supersedes" value=""><label>Note<textarea name="text" rows="3" maxlength="4000" required></textarea></label><label>For timing<select name="note_type"><option value="">Context only</option><option value="open">Open to a move</option><option value="wait">Not until a day</option></select></label><fieldset id="note-wait" hidden disabled style="border:0;padding:0;margin:0"><label>Not until<input name="wait_until" type="date" required></label></fieldset><label>Your name<input name="author" maxlength="120" required></label><button type="submit" class="button">Save note</button></form></section>`;
}
function contactPurpose(record) {
  const purpose = record?.contact_purpose;
  const label = { rapport: "Build rapport", hiring: "Discuss hiring" }[purpose] || "Purpose not assessed";
  const guidance = purpose === "rapport"
    ? "A useful professional exchange. Rapport is not evidence of job-seeking and does not justify a hiring pitch."
    : purpose === "hiring"
      ? "A proposed career conversation. Review the specific hiring reason; this does not establish willingness to change jobs."
      : "Reassess the conversation purpose before approaching. Older records are not automatically hiring recommendations.";
  return `<div class="contact-purpose"><p><strong>Proposed conversation:</strong> ${esc(label)}</p><p class="footnote">${esc(guidance)}</p></div>`;
}
function timingAssessment(event) {
  const assessment = event?.timing_assessment;
  if (!assessment) return '<div class="callout warning"><strong>Why-now assessment missing.</strong> This development still needs evidence of its effect on this person, a relevant opportunity, and the cost of waiting.</div>';
  const claims = [
    ["Why this type of conversation", event.purpose_reason],
    ["What changes for this person", assessment.person_impact],
    ["Why this conversation is useful now", assessment.opportunity],
    ["What changes if we wait", assessment.waiting_cost],
  ];
  return `<details class="section"><summary>Inspect the why-now reasoning · ${esc(human(assessment.mechanism))}</summary><p class="footnote">These are proposed interpretations of cited evidence, not verified intentions or a predicted response probability.</p>${claims.map(([label, claim]) => {
    return `<div class="evidence-item"><strong>${label}</strong><p>${esc(claim?.statement || "Not established")}</p>${claim?.quote ? `<blockquote>${esc(claim.quote)}</blockquote>` : ""}${claim?.evidence_id ? `<a class="small" href="#evidence-${esc(claim.evidence_id)}">Inspect source ${esc(claim.evidence_id)}</a>` : '<p class="footnote">No supporting source cited.</p>'}</div>`;
  }).join("")}</details>`;
}
function companyContextPanel() {
  const feed = ui.state?.company_context;
  if (ui.mode !== "live" || !feed) return "";
  return `<section class="panel"><div class="row"><h3>GI public research & company context</h3>${button("Refresh GI sources", "company-refresh")}</div><p>Ground the benefit of a conversation in what GI has actually published. Public resources do not promise private access, an introduction, or a meeting.</p><p class="footnote">${feed.current || 0} of ${feed.total || 0} sources recently retrieved · ${feed.stale_or_missing || 0} stale or missing. Rotating batches of ${feed.batch_size || 8} every ${feed.refresh_hours || 6} hours while monitoring is enabled; this is not a complete real-time social feed. Last attempt: ${stamp(feed.last_attempt_at)}.</p><details><summary>Inspect coverage and sources</summary>${list(feed.sources).map((source) => `<div class="evidence-item"><div class="row"><a href="${esc(url(source.url))}" target="_blank" rel="noopener noreferrer">${esc(source.url)} ↗</a>${badge(source.stale ? "unknown" : "verified", source.stale ? "Stale / missing" : "Retrieved")}</div><p class="footnote">${esc(human(source.source_basis || "public source"))} · Last successful retrieval: ${stamp(source.last_success_at)}${source.published_at ? " · Source publication: " + esc(source.published_at) : ""}</p>${source.error ? `<p class="small">${esc(source.error)}</p>` : ""}</div>`).join("")}<p class="footnote">Initial retrieval is a baseline. Only a later source change starts timing reassessment; the model must verify the actual development and its relevance. Failed retrieval stays a visible coverage gap.</p></details></section>`;
}
function crustdataPanel() {
  if (ui.mode !== "live") return "";
  const feed = ui.crustdata;
  if (!feed) return `<section class="panel"><h3>LinkedIn change monitoring</h3><p>${ui.crustdataError ? esc(ui.crustdataError) : "Loading provider status…"}</p></section>`;
  const subscriptions = list(feed.subscriptions), observations = list(feed.observations), gaps = list(feed.gaps), coverageErrors = list(feed.coverage_errors);
  const names = (ids) => list(ids).map((id) => ui.state.candidates.find((c) => c.id === id)?.name || id).join(", ");
  return `<section class="panel"><div class="row"><h3>LinkedIn change monitoring</h3>${badge(feed.configured ? "ready" : "unknown", feed.configured ? "Crustdata key configured" : "Crustdata key needed")}</div><p>Provider changes are research inputs. A new post or profile update does not itself justify an approach.</p><div class="actions">${button("Connect enrolled profiles", "crustdata-connect", "", feed.configured ? "" : "disabled")}${button("Check provider updates", "crustdata-sync", "", feed.configured ? "" : "disabled")}${!feed.configured ? '<button class="button" type="button" data-view="setup">Open Setup</button>' : ""}</div><p class="footnote">Connect uses exact LinkedIn URLs already associated with enrolled people and reuses existing subscriptions. Provider calls may incur charges. ${esc(feed.note || "")}</p>${feed.account?.error ? `<p class="connection-error">Account check: ${esc(feed.account.error)}</p>` : ""}${feed.account?.checked_at ? `<p class="footnote">Account checked ${stamp(feed.account.checked_at)}${feed.account.credits != null ? " · Credits reported: " + esc(typeof feed.account.credits === "object" ? JSON.stringify(feed.account.credits) : feed.account.credits) : ""}</p>` : ""}${ui.crustdataReport ? `<p class="callout">${esc(ui.crustdataReport)}</p>` : ""}${coverageErrors.length ? `<div class="callout warning"><strong>Recent provider coverage errors</strong>${coverageErrors.slice(-3).reverse().map((entry) => `<p class="small">${esc(entry.coverage_error)}</p>`).join("")}<p class="footnote">Incomplete retrieval is a coverage gap, not evidence that nothing changed.</p></div>` : ""}<details><summary>${subscriptions.length} provider subscriptions · ${gaps.length} coverage gaps · ${observations.length} recorded observations</summary>${gaps.map((gap) => `<p class="small"><strong>${esc(gap.name || names([gap.candidate_id]))}:</strong> ${esc(gap.reason)}</p>`).join("")}${subscriptions.map((sub) => `<div class="evidence-item"><div class="row"><a href="${esc(url(sub.profile_url))}" target="_blank" rel="noopener noreferrer">${esc(sub.profile_url || sub.id)} ↗</a>${badge(sub.status)}</div><p class="small">${esc(human(sub.dataset))} · Last local retrieval: ${stamp(sub.last_polled_at)}</p><p class="footnote">Watch evaluation: ${sub.config?.trigger?.every_hours != null ? "every " + esc(sub.config.trigger.every_hours) + " hour(s)" : "cadence not reported"}. ${sub.dataset === "person" ? `Profile refresh: ${sub.config?.refresh_frequency_days != null ? "every " + esc(sub.config.refresh_frequency_days) + " day(s)" : "cadence not reported"}. Hourly evaluation does not mean hourly profile refresh.` : "Post availability depends on provider collection and indexing; evaluation cadence does not guarantee real-time detection."}</p>${sub.error ? `<p class="connection-error">${esc(sub.error)}</p>` : ""}</div>`).join("") || '<p class="muted">No provider subscriptions yet. A configured key does not mean monitoring is active.</p>'}${observations.length ? '<h4>Latest provider observations</h4>' : ""}${observations.slice(0, 10).map((observation) => `<div class="evidence-item"><div class="row"><strong>${esc(names(observation.candidate_ids) || "Person association unresolved")}</strong>${badge(observation.baseline ? "watch" : observation.status, observation.baseline ? "Baseline · not a trigger" : human(observation.status))}</div><p>${esc(observation.summary || human(observation.kind))}</p><p class="footnote">${observation.event_date_basis === "post_publication_only" ? "Provider-reported post publication: " + date(observation.event_date) + " · Underlying career/project event date: not established" : "Underlying event date: " + date(observation.event_date)} · Detected locally: ${stamp(observation.detected_at)}</p>${observation.source_url ? `<a class="small" href="${esc(url(observation.source_url))}" target="_blank" rel="noopener noreferrer">Inspect source ↗</a>` : ""}</div>`).join("")}<p class="footnote">A post publication date does not establish when its underlying career or project event occurred. An unknown event date stays unknown. Detection time is not the time someone became reachable. Initial retrieval establishes a baseline; qualifying later changes can start research.</p></details></section>`;
}
function signalPlan(plan) {
  const signals = list(plan.signal_plan?.signals || plan.signal_plan);
  return `<details><summary>${esc(plan.title || roleTitle(plan.id))}</summary>${signals.length ? signals.map((signal) => typeof signal === "string" ? `<p>${esc(signal)}</p>` : `<div class="evidence-item"><strong>${esc(signal.name || signal.signal || signal.kind || signal.title || "Signal hypothesis")}</strong>${Object.entries(signal).filter(([key]) => !["id", "name", "signal", "kind", "title"].includes(key)).map(([key, value]) => `<p class="small"><strong>${esc(human(key))}:</strong> ${esc(typeof value === "string" ? value : Array.isArray(value) ? value.join("; ") : JSON.stringify(value))}</p>`).join("")}</div>`).join("") : '<p class="small muted">No structured signal plan yet. A missing plan is a coverage gap.</p>'}</details>`;
}
function sequenceAlert(alert) {
  const ping = alert.ping || {}, person = ping.person || {}, trigger = ping.trigger || {}, confidence = ping.confidence || {}, draft = ping.draft_outreach || {};
  const status = alert.status || "proposed";
  return `<article class="panel" data-sequence-alert-id="${esc(alert.id)}"><div class="row"><h3><a href="${esc(url(person.public_profile_url))}" target="_blank" rel="noopener noreferrer">${esc(person.name || ui.state.candidates.find((c) => c.id === alert.candidate_id)?.name || "Person")} ↗</a></h3>${badge(status, status === "proposed" ? "Proposed · review required" : human(status))}</div><p class="small muted">Saved to inbox ${stamp(alert.created_at)} · ${alert.seen_at ? "Displayed by browser " + stamp(alert.seen_at) : "Browser display receipt pending"}</p><p><strong>Role:</strong> ${esc(ping.role?.title || "Unknown")}</p><p><strong>Trigger:</strong> ${esc(trigger.what_changed || "Missing trigger")} · ${date(trigger.date)}</p><p class="small">${list(trigger.source_urls).map((source, index) => `<a href="${esc(url(source))}" target="_blank" rel="noopener noreferrer">Source ${index + 1} ↗</a>`).join(" · ")}</p>${contactPurpose(ping)}<p><strong>Why now:</strong> ${esc(ping.why_now || "Not established")}</p>${ping.purpose_reason?.statement ? `<p><strong>Why this conversation:</strong> ${esc(ping.purpose_reason.statement)}</p>${ping.purpose_reason.quote ? `<blockquote>${esc(ping.purpose_reason.quote)}</blockquote>` : ""}` : ""}<p><strong>Confidence:</strong> timing ${esc(human(confidence.timing_evidence))}; fit ${esc(human(confidence.fit))}; route ${esc(human(confidence.route))}. ${esc(confidence.reasoning || "")}</p><p><strong>What would prove this wrong:</strong> ${esc(list(confidence.what_would_prove_wrong).join(" ") || "Not established")}</p><p><strong>Route in:</strong> ${esc(ping.route_in?.description || "none found")}</p><details><summary>Draft outreach · ${esc(draft.sender?.proposed_function || "Sender needs confirmation")}</summary><div class="draft"><div class="draft-subject">${esc(draft.subject || "Missing subject")}</div><div class="draft-body">${esc(draft.body || "Missing draft")}</div></div></details><p class="footnote">${status === "withdrawn" ? "This alert no longer supports an approach. Open the current record before making any decision." : status === "approved" ? "Internal review recorded. Outreach still requires the sender’s decision." : "A research recommendation, not approval to contact. Verify identity, fit, source claims, timing and the proposed sender."}</p>${button("Open current evidence & review", "sequence-person", "", `data-person-id="${esc(alert.candidate_id)}"`)}</article>`;
}
function sequenceDemoResult(result) {
  const labels = { baseline: "Baseline", unchanged: "Unchanged", meaningful_change: "Meaningful change", repeat: "Repeated", opt_out: "Opt-out" };
  const amount = (value) => Number.isFinite(value) ? value : "Not reported";
  return `<section class="panel"><h2>Demonstration result</h2><p>A fictional replay across three roles, using the real decision and inbox mechanisms. It does not measure real candidate accuracy and sends no candidate messages.</p>${list(result.steps).map((step, index) => {
    const outcome = step.stage === "opt_out"
      ? (step.suppressed === true ? "Suppressed" : step.suppressed === false ? "Suppression failed" : "Not reported")
      : step.stage === "meaningful_change"
        ? `${amount(step.persisted)} alerts persisted`
        : `${amount(step.new_alerts)} new alerts`;
    return `<div class="help-step"><span class="step-number">${index + 1}</span><div><h3>${esc(labels[step.stage] || human(step.stage))} · ${esc(outcome)}</h3><p>${esc(step.outcome || "")}</p></div></div>`;
  }).join("")}<p class="footnote">Persisted means saved to the internal inbox. A separate browser display receipt records when an alert is rendered, not whether a person read or approved it.</p></section>`;
}

function sequencePage() {
  const sequence = ui.sequence;
  if (!sequence) return `${heading("Watches & alerts", "Find a defensible moment to approach a person GI already has reason to want.")}<div class="content"><div class="panel"><p>${ui.sequenceError ? esc(ui.sequenceError) : "Loading the watch sequence…"}</p>${button("Retry", "refresh")}</div></div>`;
  const watches = list(sequence.watches), observations = list(sequence.observations), alerts = list(sequence.alerts);
  return `${heading("From interest to the right moment", "Watch a small, deliberate list. Surface a dated change only when it supports a useful conversation now.", `${ui.mode === "simulation" ? button("Run timing demonstration", "sequence-demo") + button("Reset synthetic data", "reset-simulation", "danger") : ""}${ui.mode === "live" ? button(sequence.monitoring_enabled ? "Pause background monitoring" : "Enable background monitoring", "sequence-monitor", "", `data-enabled="${!sequence.monitoring_enabled}"`) + button("Check enrolled people", "sequence-run", "primary") : ""}`)}<div class="content">${ui.sequenceReceiptError ? `<div class="callout warning">${esc(ui.sequenceReceiptError)}</div>` : ""}<div class="callout" style="margin-bottom:18px"><strong>The goal is better timing.</strong> A new page or impressive profile is not enough. Each alert needs a meaningful change, a specific reason to approach now, evidence, and a usable note. ${ui.mode === "simulation" ? "Use the timing demonstration to observe fictional changes without provider calls." : sequence.monitoring_enabled ? "Background monitoring is enabled while this server runs." : "Background monitoring is off. Use a manual check, or enable it above. Provider calls can incur charges."}</div>${ui.sequenceDemo ? sequenceDemoResult(ui.sequenceDemo) : ""}<section class="panel"><div class="row"><h2>1 · Choose who is worth watching</h2>${button("Choose from candidates", "watchlist")}</div><p>Enrollment records why GI might want this person. It does not verify every hiring requirement or authorize contact.</p>${watches.length ? watches.map((watch) => `<div class="evidence-item"><div class="row"><strong>${esc(watch.name || ui.state.candidates.find((c) => c.id === watch.candidate_id)?.name || watch.candidate_id)}</strong>${badge(watch.status)}</div><p class="small muted">${esc(roleTitle(watch.role_id))}</p><p>${esc(watch.fit_basis)}</p><p class="small"><strong>Coverage:</strong> ${esc(human(watch.coverage_status || "not checked"))} · ${list(watch.urls).length} chosen sources</p><p class="footnote">Last check: ${stamp(watch.last_checked_at)} · Next source check: ${stamp(watch.next_check_at)}${watch.last_outcome ? " · " + esc(typeof watch.last_outcome === "string" ? human(watch.last_outcome) : JSON.stringify(watch.last_outcome)) : ""}</p><div class="actions">${button("Open person", "sequence-person", "", `data-person-id="${esc(watch.candidate_id)}"`)}${button("Check sources", "watch-check", "", `data-watch-id="${esc(watch.id)}" ${watch.status !== "active" || ui.mode === "simulation" ? "disabled" : ""}`)}${button(watch.status === "active" ? "Pause" : "Resume", "watch-toggle", "tertiary", `data-watch-id="${esc(watch.id)}" data-status="${watch.status === "active" ? "paused" : "active"}"`)}</div></div>`).join("") : '<p class="muted">No enrolled watches yet. Open a candidate and select “Watch this person.”</p>'}</section><section class="panel"><div class="row"><h2>2 · Define the changes that matter</h2>${button("Drop in a JD", "role-drop")}</div><p>Role-specific signal hypotheses make the watch explicit. Company changes are context; tenure and a mutual follow alone do not establish receptivity.</p>${list(sequence.plans).map(signalPlan).join("") || '<p class="muted">No role plans available.</p>'}</section>${companyContextPanel()}${crustdataPanel()}<section class="panel"><h2>3 · Observe changes and gaps</h2><p>First retrieval establishes a baseline. Failed retrieval means unknown coverage. Unchanged pages should stay quiet.</p>${observations.length ? `<div class="table-scroll"><table><thead><tr><th>Person</th><th>Checked</th><th>Observation</th></tr></thead><tbody>${observations.slice(0, 20).map((observation) => `<tr><td>${esc(ui.state.candidates.find((c) => c.id === observation.candidate_id)?.name || observation.candidate_id)}</td><td>${stamp(observation.checked_at)}</td><td>${esc(observation.summary || "Observation recorded")}<div class="footnote">${Array.isArray(observation.changed_sources) ? observation.changed_sources.length : Number(observation.changed_sources || 0)} changed sources</div></td></tr>`).join("")}</tbody></table></div>` : '<p class="muted">No watch observations recorded yet.</p>'}</section><section><div class="section-title"><h2>4 · Receive the moment and the note</h2><span class="small muted">Internal review inbox</span></div>${alerts.length ? alerts.map(sequenceAlert).join("") : '<div class="panel"><p>No complete timing alerts yet. Silence can be the correct result; inspect coverage and quiet cases before treating it as success.</p></div>'}</section></div>`;
}
function watchDialog() {
  const candidate = current();
  if (!candidate) throw new Error("Choose a person first.");
  openDialog("Watch this person", `<p>Explain why GI should pay attention to ${esc(candidate.name)}. This enrolls professional sources for monitoring; it is not a hiring verdict or permission to contact them.</p><form id="watch-form" data-person-id="${esc(candidate.id)}"><div class="form-group"><label for="watch-fit">Why is this person worth watching for this role?</label><textarea id="watch-fit" name="fit_basis" required minlength="20" rows="4" placeholder="Relevant demonstrated work, hiring context, and any unresolved fit questions."></textarea></div><div class="form-group"><label for="watch-organization">Current organization to investigate (optional)</label><input id="watch-organization" name="organization" value="${esc(candidate.employer || "")}"><span class="hint">Unverified context for company or team changes; this field does not establish employment.</span></div><div class="form-group"><label for="watch-urls">Professional sources to monitor</label><textarea id="watch-urls" name="urls" rows="5">${esc(candidate.profile_url || "")}</textarea><span class="hint">Up to eight public professional source URLs, one per line. Add an announcement feed, personal work page, or relevant company page. Private or unrelated personal activity is outside this watch.</span></div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Enroll watch</button></div></form>`);
}
function roleDropDialog() {
  const intro = `<p>The engine reads the JD into a role: its reasons to reach out, its reasons to wait, and what it can't watch. Nothing is written until you add the role.</p><p class="hint">New posts on GI and Medal's job board aren't read on their own yet. ${button("See what that would look like", "board-open", "tertiary board-link")}</p>`;
  if (ui.mode === "simulation")
    return openDialog("Drop in a job description", `${intro}<p class="hint">Simulation reads only an invented sample JD, with answers written by hand in the model's format, for free. Switch to live to drop in a real one.</p><div class="dialog-actions">${button("Cancel", "close")}${button("Try the sample JD", "role-sample", "primary")}</div>`);
  openDialog("Drop in a job description", `${intro}<form id="role-drop-form"><label class="drop-zone" for="drop-file"><strong>Drop a .txt or .md file here</strong> or choose one, or paste the JD below.<input id="drop-file" type="file" accept=".txt,.md,text/plain,text/markdown"></label><div class="form-group"><label for="drop-jd">Job description</label><textarea id="drop-jd" name="jd_text" rows="9" minlength="40" required></textarea></div><div class="form-group"><label for="drop-url">Link to its job post (optional)</label><input id="drop-url" name="jd_url" type="url" placeholder="https://jobs.ashbyhq.com/…"></div><div class="form-group"><label for="drop-offices">Offices (optional)</label><input id="drop-offices" name="locations" placeholder="New York City, London"></div><p class="hint">${esc(ui.state.drop_price || "")}</p><div class="dialog-actions">${button("Cancel", "close")}${button("Try the sample JD", "role-sample")}<button class="button primary" type="submit">Read this JD</button></div></form>`);
}
function readDroppedFile(file) {
  if (!file) return;
  if (!/\.(txt|md)$/i.test(file.name)) return toast("Drop a .txt or .md file. PDF and Word files aren't read.", true);
  file.text().then((text) => { $("#drop-jd").value = text; });
}
function watchPlan(w) {
  return `<h3>Reasons to reach out for this role</h3><ul>${list(w.signs).map((s) => `<li><strong>${esc(s.means)}</strong> ${esc(s.why)}</li>`).join("") || "<li>Nothing beyond what counts for every role.</li>"}</ul><p>${esc(w.every_role)}</p><h3>Reasons to wait</h3><p>${esc(w.holds)}</p><h3>Close to its work</h3><p>${list(w.topics).map(esc).join(" · ") || "No topics: posts are read for GI's work only."}</p><h3>What it can't watch</h3><ul>${list(w.gaps).map((g) => `<li>${esc(g)}</li>`).join("") || "<li>No gaps listed.</li>"}</ul>`;
}
function rolePreviewDialog(p) {
  ui.rolePreview = p;
  const cost = p.cost ? `<p class="hint">${p.cost.usd == null ? "This read's cost couldn't be priced." : `This read cost $${p.cost.usd.toFixed(2)}.`}</p>` : "";
  openDialog(`${esc(p.role.title)}: what the engine would watch`, `${p.sample ? `<div class="callout">Invented sample JD, with answers written by hand in the model's format. It previews; it never becomes a role.</div><details><summary>The JD it read</summary><pre class="link-wrap">${esc(p.jd_text)}</pre></details>` : ""}${p.clash ? `<div class="callout warning">${esc(p.clash)} Nothing will be added.</div>` : ""}${watchPlan(p.watch)}<h3>Before its calls go out</h3><ul>${list(p.before).map((b) => `<li>${esc(b)}</li>`).join("")}</ul>${cost}<form id="role-accept-form"><div class="dialog-actions">${button("Close", "close")}${p.sample || p.clash ? "" : `<button class="button primary" type="submit">Add ${esc(p.role.title)}</button>`}</div></form>`);
  $("#dialog").scrollTop = 0; // opens at its top, not where the drop box was scrolled
}

function rolesPage() {
  return `${heading("Living role briefs", "Give the engine the hiring context a job description misses.", button("Drop in a JD", "role-drop", "primary"))}<div class="content"><div class="callout" style="margin-bottom:18px">Role briefs are shared across live and simulation workspaces. Saving changes future discovery and re-evaluates existing candidates. Earlier approvals must be reviewed again.</div>${list(
    ui.state.roles,
  )
    .map(
      (r) =>
        `<form class="panel role-form" data-role-id="${esc(r.id)}" data-version="${esc(r.brief_version)}"><div class="row"><h2>${esc(r.title)}</h2><span class="small muted">Version ${esc(r.brief_version)}</span></div>${r.jd_url ? `<p><a href="${esc(url(r.jd_url))}" target="_blank" rel="noopener noreferrer">Read job description ↗</a></p>` : ""}<div class="form-group"><label for="criteria-${esc(r.id)}">What this person must demonstrate</label><textarea id="criteria-${esc(r.id)}" name="criteria" rows="5">${esc(list(r.criteria).join("\n"))}</textarea><span class="hint">One criterion per line. Describe the work and evidence, not just titles.</span></div><div class="form-group"><label for="notes-${esc(r.id)}">Hiring-manager context</label><textarea id="notes-${esc(r.id)}" name="manager_notes" rows="4" placeholder="Relevant industries, acceptable alternatives, founder experience, and examples of strong or weak fit…">${esc(r.manager_notes || "")}</textarea></div><div class="actions"><button class="button primary" type="submit">Save brief & invalidate approvals</button>${button("Discover for this role", "discover", "", 'data-role="' + esc(r.id) + '"')}${r.implementation_stage === "dropped" ? button("Remove this role", "role-remove", "danger", `data-role="${esc(r.id)}" data-title="${esc(r.title)}"`) : ""}</div>${r.watch ? `<details><summary>What the engine watches for this role</summary>${watchPlan(r.watch)}</details>` : ""}</form>`,
    )
    .join("")}</div>`;
}
// What watching GI and Medal's job board would look like: fixed, invented, and labelled so. Nothing on it is live and it
// fetches nothing. Today a JD comes in only through Drop in a JD, and people join the watchlist by hand.
const BOARD = {
  post: "Senior Frontend Engineer (React)", // a title on GI and Medal's board that isn't a role here yet
  counts: ["A launch of their own work", "Work in progress in public", "A technical ask", "A shift in what they post about"],
  holds: "A first year in a new job, a recent promotion, new equity, their own launch week, \"not looking\", a next role already settled",
  people: [
    { name: "Ines Varga", item: "Posted a clip of a replay viewer for game clips, still in progress, built in React", where: "X", when: "yesterday", call: ["reach", "Reach out now"], why: "Early sign: work in progress close to GI's work, in their own post." },
    { name: "Tomás Ferro", item: "Released version 2.0 of an open-source canvas library", where: "GitHub", when: "4 days ago", call: ["wait", "Wait"], why: "Their own launch week: the engine waits 3 more days, until it's over." },
    { name: "Dana Okafor", item: "Posted about input lag in cloud games", where: "LinkedIn", when: "6 days ago", call: ["quiet", "Stay quiet"], why: "Nothing yet that counts for this role." },
  ],
};
function boardPage() {
  const step = (when, title, body, cls = "") => `<li class="board-step ${cls}"><span class="board-when">${when}</span><span class="board-dot" aria-hidden="true"></span><div class="board-body"><h3>${title}</h3>${body}</div></li>`;
  const person = (p) => `<div class="board-person" data-person="${esc(p.name)}"><div class="row"><strong>${esc(p.name)}</strong>${badge("invented", "Invented")}</div><p class="board-item">${esc(p.item)}</p><p class="small muted">On ${esc(p.where)}, ${esc(p.when)}</p><div class="board-call">${badge(p.call[0], p.call[1])}<span class="small">${esc(p.why)}</span></div></div>`;
  return `<div class="board-watch"><div class="callout warning board-banner"><strong>Preview: not built yet.</strong> This page is fixed and made up. Nothing watches GI and Medal's job board for new posts today (it is read only for posted pay ranges), no JD is read on its own, and the people below are invented. Today a JD comes in through Drop in a JD, and people join the watchlist by hand.</div><div class="page-heading"><div><h1>New job posts</h1><p>What it would look like if each new post on GI and Medal's job board became a role on its own, and the people watched for it were read each morning.</p></div><div class="actions"><button class="button" type="button" data-view="roles">Back to Role briefs</button>${button("Drop in a JD", "role-drop", "primary")}</div></div><div class="content"><ol class="board-steps">${[
    step("07:30", "Checks GI and Medal's job board", `<p>One post that wasn't there yesterday.</p><p class="small muted">jobs.ashbyhq.com/generalintuition-medal</p>`),
    step("07:31", "New post spotted", `<div class="board-post"><span data-post="${esc(BOARD.post)}">${esc(BOARD.post)}</span>${badge("happened", "New on the board")}</div><p class="small muted">Not a role here yet.</p>`),
    step("07:32", "Reads the JD", `<p>The same read as Drop in a JD, with nobody pasting: two model calls, about $0.33 (what the last real read cost).</p>`),
    step("07:34", "Role ready", `<p class="small muted">An illustration, not a real read.</p><div class="board-chips"><span class="board-label">Reasons to reach out</span>${BOARD.counts.map((c) => `<span class="board-chip">${esc(c)}</span>`).join("")}</div><div class="board-chips"><span class="board-label">Reasons to wait</span><span class="board-chip hold">${esc(BOARD.holds)}</span></div><p class="small muted">Its calls haven't been checked yet: that takes made-up cases for this role with the right call written down, and a look back at how its calls would have done.</p>`),
    step("Once people are added", `Watching ${BOARD.people.length} people`, `<p class="small muted">Added by hand, once a quick check shows they post enough in public to read and the profiles are really theirs. Then read each morning: their new public posts and code, and news about their employer.</p><div class="board-people">${BOARD.people.map(person).join("")}</div>`, "live"),
    step("Next morning 07:30", "Next read", `<p>The board again, and these people's new posts and code.</p>`, "next"),
  ].join("")}</ol><div class="panel board-status"><div class="board-col"><h2>Built</h2><ul><li>Reading a JD into a role: Drop in a JD on Role briefs.</li><li>The morning read of watched people, and their calls on Today's calls.</li></ul></div><div class="board-col"><h2>Not built yet</h2><ul><li>Checking GI and Medal's job board for new posts, and reading them without anyone pasting.</li><li>Finding people for a new role: today they are added by hand.</li></ul></div></div></div></div>`;
}
function sourceExcerpt(s) {
  const text = esc(s.excerpt || "No excerpt available");
  const additional = list(s.additional_excerpts).map((quote) => `<p class="footnote">Separate passage from the same source</p><blockquote>${esc(quote)}</blockquote>`).join("");
  return s.source_kind === "exa_agent_grounding"
    ? `<p class="small muted">Discovery citation · source text still needs retrieval</p><p>${text}</p>`
    : `<blockquote>${text}</blockquote>${additional}`;
}
function runDetails(j) {
  const result = j.result || {};
  const run = j.remote_run || {};
  const grounding = list(run.output?.grounding);
  const links = new Map();
  grounding.forEach((g) =>
    list(g.citations).forEach((c) => {
      if (url(c.url) !== "#") links.set(c.url, c.title || c.url);
    }),
  );
  return `${j.error ? `<p class="connection-error">${esc(j.error)}</p>` : ""}${j.error?.includes("retained for review") ? `<p><a href="/api/jobs/${encodeURIComponent(j.id)}/research-proposal" target="_blank" rel="noopener noreferrer">Inspect retained research proposal ↗</a></p>` : ""}<p>${esc(result.note || (j.status === "queued" ? "Waiting for the next worker check." : ""))}</p>${run.id ? `<p class="small muted">Exa: ${esc(run.status)} · ${esc(run.id)}${run.stopReason ? ` · ${esc(run.stopReason)}` : ""}</p>` : ""}${result.candidates_added !== undefined ? `<p>${esc(result.candidates_added)} candidates added · ${esc(result.sources || 0)} cited sources</p>` : ""}${run.costDollars !== undefined ? `<p class="small">Reported Exa cost: ${esc(JSON.stringify(run.costDollars))}</p>` : ""}${links.size ? `<details><summary>${links.size} source links</summary><ul>${[...links].map(([u, t]) => `<li><a href="${esc(url(u))}" target="_blank" rel="noopener noreferrer">${esc(t)} ↗</a></li>`).join("")}</ul></details>` : ""}<details><summary>Run details & grounding</summary><pre class="link-wrap">${esc(JSON.stringify({ result, ...(run.id ? { output: run.output, warnings: run.warnings } : {}) }, null, 2))}</pre></details>`;
}
function sourcesPage() {
  return `${heading("Sources & runs", "Inspect what the engine found, what completed, and what needs attention.", button("Refresh", "refresh"))}<div class="content"><div class="panel"><div class="section-title"><h2>Recent jobs</h2>${badge(ui.state.settings?.monitoring_enabled ? "ready" : "watch", ui.state.settings?.monitoring_enabled ? "Monitoring enabled" : "Manual runs")}</div><div class="table-scroll"><table class="jobs-table"><thead><tr><th>Operation</th><th>Status</th><th>Started</th><th>Result</th></tr></thead><tbody>${
    list(ui.state.jobs)
      .map(
        (j) =>
          `<tr><td>${esc(human(j.kind))}<div class="small muted">${esc(j.id)}</div></td><td>${badge(j.status)}</td><td>${stamp(j.created_at)}</td><td>${runDetails(j)}</td></tr>`,
      )
      .join("") ||
    '<tr><td colspan="4">No jobs yet. Run discovery or evaluate signals from the inbox.</td></tr>'
  }</tbody></table></div></div><div class="panel"><h2>Shared source library</h2><p>Retrieved pages are shared research inputs across workspaces. They are not automatically verified candidate evidence.</p>${list(ui.state.sources).length ? ui.state.sources.map((s) => `<div class="evidence-item"><a class="evidence-title" href="${esc(url(s.url))}" target="_blank" rel="noopener noreferrer">${esc(s.title || s.url)} ↗</a>${sourceExcerpt(s)}<div class="evidence-meta"><span>${esc(roleTitle(s.role_id))}</span><span>Observed ${date(s.observed_at)}</span></div></div>`).join("") : '<div class="empty">Discovery results will appear here after a run.</div>'}</div><div class="panel"><h2>Delivered pings</h2><p>These are local inbox deliveries, not messages sent to candidates.</p>${list(ui.state.deliveries).length ? `<div class="table-scroll"><table><thead><tr><th>Person</th><th>Delivered</th><th>Details</th></tr></thead><tbody>${ui.state.deliveries.map((d) => `<tr><td>${esc(ui.state.candidates.find((c) => c.id === d.candidate_id)?.name || d.candidate_id)}</td><td>${stamp(d.created_at)}</td><td><details><summary>View ping</summary><pre class="link-wrap">${esc(JSON.stringify(d.ping, null, 2))}</pre></details></td></tr>`).join("")}</tbody></table></div>` : '<div class="empty">No complete pings have fired in this workspace yet.</div>'}</div></div>`;
}
function setupPage() {
  const s = ui.state.settings || {};
  return `${heading("Connect your research tools", "Settings apply to both workspaces. Keys stay on the server and are never displayed.")}<div class="content"><form class="panel" id="settings-form"><h2>Research providers</h2><p>Connect discovery and contextual research to work with real candidates. Simulation works without paid credentials.</p><div class="form-group"><div class="row"><label for="exa-key">Exa API key</label>${badge(s.exa_configured ? "ready" : "unknown", s.exa_configured ? "Configured" : "Not configured")}</div><input id="exa-key" name="exa_key" type="password" autocomplete="new-password" placeholder="Enter to add or replace key"><span class="hint">Agent discovery (up to $5 per run), search, and fresh source extraction. <a href="https://dashboard.exa.ai/" target="_blank" rel="noopener noreferrer">Open Exa dashboard ↗</a></span></div><div class="form-group"><div class="row"><label for="crustdata-key">Crustdata API key</label>${badge(s.crustdata_configured ? "ready" : "unknown", s.crustdata_configured ? "Configured" : "Not configured")}</div><input id="crustdata-key" name="crustdata_key" type="password" autocomplete="new-password" placeholder="Enter to add or replace key"><span class="hint">LinkedIn profile and post changes for enrolled people. Save the key, then connect enrolled profiles in the live Watches & alerts page.</span></div><div class="form-group"><div class="row"><label for="anthropic-key">Anthropic API key</label>${badge(s.anthropic_configured ? "ready" : "unknown", s.anthropic_configured ? "Configured" : "Not configured")}</div><input id="anthropic-key" name="anthropic_key" type="password" autocomplete="new-password" placeholder="Enter to add or replace key"><span class="hint">Contextual research and evidence-grounded drafting. <a href="https://console.anthropic.com/" target="_blank" rel="noopener noreferrer">Open Anthropic console ↗</a></span></div><div class="form-group"><label for="anthropic-workspace">Anthropic workspace ID</label><input id="anthropic-workspace" name="anthropic_workspace_id" value="${esc(s.anthropic_workspace_id || "")}" placeholder="wrkspc_…"><span class="hint">Required for identity-linked keys. Find it in Anthropic Console → Settings → Workspaces.</span></div><div class="form-row"><div class="form-group"><label for="model">Research model</label><input id="model" name="model" value="${esc(s.model || "")}" required><span class="hint">Use a model available to your provider account.</span></div><div class="form-group"><label for="max-calls">Maximum provider calls per run</label><input id="max-calls" name="max_calls_per_run" type="number" min="${esc(s.max_calls_range?.[0] ?? "")}" max="${esc(s.max_calls_range?.[1] ?? "")}" value="${esc(s.max_calls_per_run ?? "")}"><span class="hint">A call limit bounds activity; provider prices determine actual cost.</span></div></div><label class="check"><input name="monitoring_enabled" type="checkbox" ${s.monitoring_enabled ? "checked" : ""}><span>Enable background monitoring while the server is running.</span></label><div class="callout warning">Live discovery and research can incur provider charges. Start with one role and inspect the results in Sources & runs.</div><div class="actions" style="margin-top:19px"><button class="button primary" type="submit">Save settings</button></div></form>${companyContextPanel()}${crustdataPanel()}<div class="panel"><h2>What still needs your input</h2><p>Confirm the role briefs, add any authorized warm relationships, and verify the source evidence for candidates you want to approach. Assign an actual senior sender in the candidate editor before using outreach.</p><p>Private investor talent networks require your own authorized access. Public profile links and real introductions can be entered manually; a mutual follow alone is not a verified warm path.</p></div></div>`;
}
function openDialog(title, body) {
  $("#dialog-content").innerHTML =
    `<div class="dialog-head"><h2>${title}</h2>${button("✕", "close", "tertiary", 'aria-label="Close dialog"')}</div><div class="dialog-body">${body}<div id="dialog-error" class="error-text" role="alert"></div></div>`;
  $("#dialog").showModal();
}
function closeDialog() {
  $("#dialog").close();
  ui.dirty = false;
  ui.editCandidate = null;
}
function editor(c) {
  const value = c
    ? structuredClone(c)
    : {
        name: "",
        profile_url: "",
        role_id: ui.role === "all" ? ui.state.roles[0]?.id : ui.role,
        mode: ui.mode,
        summary: "",
        employer: "",
        location: "",
        identity_status: "unknown",
        fit: [],
        evidence: [],
        events: [],
        route: {
          status: "none_found",
          description: "No warm path found.",
          evidence_ids: [],
        },
        draft: {
          subject: "",
          body: "",
          sender_function: "",
          sender_name: "",
          channel: "email",
          source_ids: [],
        },
      };
  delete value.decision;
  openDialog(
    c ? "Edit research record" : "Add a person",
    `<p>Record professional facts and source evidence. Changes invalidate earlier review. Use the JSON editor for this first pilot.</p><form id="candidate-form" data-id="${esc(c?.id || "")}"><label class="small" for="candidate-json">Candidate record</label><textarea id="candidate-json" class="code-editor" spellcheck="false">${esc(JSON.stringify(value, null, 2))}</textarea><details class="small"><summary>Evidence and event format</summary><p>Evidence: id, url, title, excerpt, observed_at, event_date, verified, source_kind.<br>Event: id, kind, title, date, timing_action (contact_now, watch, follow_up), contact_purpose (rapport, hiring, unassessed), purpose_reason (statement, evidence_id, quote), recipient_value, follow_up_on (only for an explicit source-backed request or constraint), follow_up_quote, why_now, why_wait, falsifier, evidence_ids, conversation_score, career_openness, confidence.</p></details><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Save research</button></div></form>`,
  );
}

function routeDialog() {
  ui.editCandidate = structuredClone(current());
  const r = current().route || {};
  openDialog(
    "Edit the route into a conversation",
    `<p>Keep a possible connection distinct from a verified relationship. A shared follow alone does not establish a willing introducer.</p><form id="route-form"><div class="form-group"><label for="route-status">Relationship status</label><select id="route-status" name="status">${[
      ["none_found", "No warm path found"],
      ["plausible_unconfirmed", "Possible relationship, unconfirmed"],
      ["verified", "Verified relationship"],
    ]
      .map(
        ([v, t]) =>
          `<option value="${v}" ${r.status === v ? "selected" : ""}>${t}</option>`,
      )
      .join(
        "",
      )}</select></div><div class="form-group"><label for="route-introducer">Potential introducer</label><input id="route-introducer" name="introducer" value="${esc(r.introducer || "")}"></div><div class="form-group"><label for="route-description">What is the relationship?</label><textarea id="route-description" name="description" rows="4">${esc(r.description || "")}</textarea><span class="hint">Describe what is verified and whether this person has actually agreed to introduce you.</span></div><div class="form-group"><label>Relationship evidence</label>${
      list(current().evidence)
        .map(
          (e) =>
            `<label class="check"><input type="checkbox" name="route_sources" value="${esc(e.id)}" ${list(r.evidence_ids).includes(e.id) ? "checked" : ""}><span>${esc(e.title || e.id)}</span></label>`,
        )
        .join("") ||
      '<span class="hint">Use Add evidence to record the relationship source first.</span>'
    }</div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Save route</button></div></form>`,
  );
}

function evidenceDialog() {
  const c = current();
  ui.editCandidate = structuredClone(c);
  const role = ui.state.roles.find((r) => r.id === c.role_id);
  openDialog(
    "Add evidence or a timing signal",
    `<p>Keep the source claim separate from your timing interpretation. This record stays unverified until you review it.</p><form id="evidence-form"><div class="form-group"><label for="evidence-url">Source URL</label><input id="evidence-url" name="url" type="url" required placeholder="https://…"></div><div class="form-group"><label for="evidence-title">Source title</label><input id="evidence-title" name="title" required placeholder="Professional profile, announcement, or publication"></div><div class="form-group"><label for="evidence-excerpt">Exact supporting excerpt</label><textarea id="evidence-excerpt" name="excerpt" rows="4" required maxlength="2000" placeholder="Copy only the passage that supports your claim."></textarea></div><div class="form-group"><label for="evidence-date">Professional development date supported by this source</label><input id="evidence-date" name="event_date" type="date"><span class="hint">A professional development may be a release, role change, or announcement; it does not necessarily mean an attended event. Leave unknown for fit-only evidence. Do not use today's date for an undated past development.</span></div><div class="form-group"><label>Which requirements does this evidence support?</label>${list(
      role?.criteria,
    )
      .map(
        (x) =>
          `<label class="check"><input type="checkbox" name="supports" value="${esc(x)}"><span>${esc(x)}</span></label>`,
      )
      .join(
        "",
      )}<span class="hint">Select only directly supported claims. This updates the corresponding fit assessment.</span></div><label class="check"><input id="include-event" type="checkbox" name="include_event"><span>This also creates a specific opportunity for a conversation.</span></label><fieldset id="event-fields" hidden disabled style="border:0;padding:0;margin:0"><div class="form-group"><label for="event-title">What changed?</label><input id="event-title" name="event_title" required placeholder="The specific professional change"></div><div class="form-row"><div class="form-group"><label for="event-kind">Signal type</label><select id="event-kind" name="kind"><option value="professional_update">Professional update</option><option value="relevant_release">Relevant release or publication</option><option value="engagement_completion">Project or engagement completed</option><option value="explicit_availability">Explicit availability</option><option value="conference">Upcoming professional event</option><option value="company_news">Company news (context only)</option><option value="passive_engagement">Social engagement (context only)</option></select></div><div class="form-group"><label for="event-confidence">Evidence confidence</label><select id="event-confidence" name="confidence"><option value="medium">Medium</option><option value="high">High</option><option value="low">Low</option></select></div></div><div class="form-group"><label for="timing-action">Proposed timing action</label><select id="timing-action" name="timing_action"><option value="watch">Watch — no sufficient reason to contact now</option><option value="contact_now">Consider contact now</option><option value="follow_up">Follow up on an explicitly supported date</option></select><span class="hint">A future conference can justify contacting now to arrange a meeting. Do not predict an ideal day or availability window.</span></div><div class="form-group"><label for="recipient-value">What could this person gain from the conversation?</label><textarea id="recipient-value" name="recipient_value" rows="2" minlength="15" placeholder="A concrete, credible benefit related to their work."></textarea></div><fieldset id="follow-up-fields" hidden disabled style="border:0;padding:0;margin:0"><div class="form-group"><label for="follow-up-on">Explicit follow-up date</label><input id="follow-up-on" name="follow_up_on" type="date" required></div><div class="form-group"><label for="follow-up-quote">Exact source excerpt supporting this date and constraint</label><textarea id="follow-up-quote" name="follow_up_quote" rows="2" required placeholder="An explicit request to reconnect or a confirmed logistical constraint."></textarea><span class="hint">This must appear in the source excerpt above. A scheduled conference alone does not mean outreach must wait until then.</span></div></fieldset><div class="form-group"><label for="why-now">Why this moment?</label><textarea id="why-now" name="why_now" rows="3" required minlength="15" placeholder="What useful exchange is possible now, and why is this time-sensitive?"></textarea></div><div class="form-group"><label for="why-wait">What is the case for waiting?</label><textarea id="why-wait" name="why_wait" rows="2" required minlength="15"></textarea></div><div class="form-group"><label for="falsifier">What would prove this wrong?</label><textarea id="falsifier" name="falsifier" rows="2" required minlength="15"></textarea></div><div class="form-row"><div class="form-group"><label for="conversation-score">Conversation opportunity</label><select id="conversation-score" name="conversation_score"><option value="0">0 — No useful opening established</option><option value="1">1 — Possible, weak evidence</option><option value="2">2 — Supported, worthwhile topic</option><option value="3">3 — Strong, specific opening</option></select></div><div class="form-group"><label for="career-openness">Career openness</label><select id="career-openness" name="career_openness"><option value="unknown">Unknown</option><option value="possible">Possible</option><option value="explicit">Explicitly stated</option></select></div></div></fieldset><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Add evidence</button></div></form>`,
  );
}

function updateNoteWait(form) {
  const field = form.querySelector("#note-wait");
  field.hidden = field.disabled = form.elements.note_type.value !== "wait";
}

function updateTimingFields() {
  const action = $("#timing-action")?.value;
  const enabled = $("#include-event")?.checked;
  const followUpFields = $("#follow-up-fields");
  if (followUpFields) {
    followUpFields.hidden = action !== "follow_up";
    followUpFields.disabled = !enabled || action !== "follow_up";
  }
  const recipientValue = $("#recipient-value");
  if (recipientValue) recipientValue.required = Boolean(enabled && action === "contact_now");
}

function aliasDialog() {
  const c = current();
  openDialog(
    "Compare possible duplicate identities",
    `<p>A matching name is only a clue. Open both profiles and compare their work, employers, and biographical details. Link only if they are the same person.</p><div class="callout"><strong>${esc(c.name)}</strong><br><a href="${esc(url(c.profile_url))}" target="_blank" rel="noopener noreferrer">${esc(c.profile_url)}</a></div>${list(
      c.possible_aliases,
    )
      .map((id) => {
        const other = ui.state.candidates.find((x) => x.id === id);
        return other
          ? `<div class="section"><h3>${esc(other.name)}</h3><p>${esc(other.employer)} / ${esc(roleTitle(other.role_id))}</p><p class="link-wrap"><a href="${esc(url(other.profile_url))}" target="_blank" rel="noopener noreferrer">${esc(other.profile_url)}</a></p><div class="actions">${button("Confirm same person", "link-person", "", 'data-other="' + esc(id) + '"')}${button("Confirm different people", "distinct-person", "", 'data-other="' + esc(id) + '"')}</div></div>`
          : "";
      })
      .join(
        "",
      )}<p>If the profiles belong to different people, leave them separate and add the distinguishing evidence to the research record.</p><div class="dialog-actions">${button("Close", "close")}</div>`,
  );
}
function addPerson() {
  openDialog(
    "Add a person",
    `<p>Start with a real professional profile. Research can fill in evidence and suggest a timing hypothesis.</p><form id="add-person-form"><div class="form-row"><div class="form-group"><label for="person-name">Full name</label><input id="person-name" name="name" required minlength="2" placeholder="Full name"></div><div class="form-group"><label for="person-role">Role</label><select id="person-role" name="role_id">${list(
      ui.state.roles,
    )
      .map(
        (r) =>
          `<option value="${esc(r.id)}" ${ui.role === r.id ? "selected" : ""}>${esc(r.title)}</option>`,
      )
      .join(
        "",
      )}</select></div></div><div class="form-group"><label for="person-profile">Public profile URL</label><input id="person-profile" name="profile_url" type="url" required placeholder="https://…"></div><div class="form-group"><label for="person-summary">What makes them interesting?</label><textarea id="person-summary" name="summary" rows="3" placeholder="Relevant work, a nomination, or something to investigate…"></textarea><span class="hint">Treat unverified notes as research leads, not established evidence.</span></div><div class="dialog-actions">${button("Import full JSON", "import-json", "tertiary")}${button("Cancel", "close")}<button class="button primary" type="submit">Add to watchlist</button></div></form>`,
  );
}
function editDraft() {
  ui.editCandidate = structuredClone(current());
  const d = current()?.draft || {};
  openDialog(
    "Edit the approach",
    `<p>Choose the actual senior sender and make the message specific to the evidence. Saving invalidates earlier approval.</p><form id="draft-form"><div class="form-row"><div class="form-group"><label for="sender-function">Sender role</label><input id="sender-function" name="sender_function" value="${esc(d.sender_function || "")}" required></div><div class="form-group"><label for="sender-name">Sender name</label><input id="sender-name" name="sender_name" value="${esc(d.sender_name || "")}" placeholder="Confirmed sender"></div></div><div class="form-group"><label for="draft-channel">Channel</label><select id="draft-channel" name="channel">${["email", "linkedin", "x", "introduction"].map((v) => `<option value="${v}" ${d.channel === v ? "selected" : ""}>${esc(human(v))}</option>`).join("")}</select></div><div class="form-group"><label for="draft-subject">Subject</label><input id="draft-subject" name="subject" value="${esc(d.subject || "")}" required></div><div class="form-group"><label for="draft-body">Message</label><textarea id="draft-body" name="body" rows="9" required>${esc(d.body || "")}</textarea></div><div class="form-group"><label>Sources supporting this message</label>${
      list(current()?.evidence)
        .map(
          (e) =>
            `<label class="check"><input type="checkbox" name="draft_sources" value="${esc(e.id)}" ${list(d.source_ids).includes(e.id) ? "checked" : ""}><span>${esc(e.title || e.id)}</span></label>`,
        )
        .join("") ||
      '<span class="hint">Add evidence first, then link it to the draft.</span>'
    }</div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Save draft</button></div></form>`,
  );
}

async function run(operation, roleId) {
  const body = { operation, mode: ui.mode };
  if (roleId || ui.role !== "all") body.role_id = roleId || ui.role;
  if (operation === "research") {
    if (!current()) throw new Error("Choose a person to research.");
    body.candidate_id = current().id;
  }
  if (operation === "discover" && ui.mode === "simulation") {
    toast(
      "Discovery uses real providers. Switch to Live data, or test the seeded simulation candidates.",
    );
    return;
  }
  const result = await api("/api/run", "POST", body);
  toast(result.message || "Run queued. Inspect progress in Sources & runs.");
  await refresh(true);
}
async function candidateAction(action, extra = {}) {
  if (!current()) throw new Error("Select a person first.");
  await api(
    `/api/candidates/${encodeURIComponent(current().id)}/action`,
    "POST",
    { action, ...extra },
  );
  opsChanged(); // an opt-out or a send is in the ledger every ops page reads
  closeDialog();
  toast("Saved. The decision has been refreshed.");
  await refresh(true);
}
document.addEventListener("click", async (ev) => {
  const el = ev.target.closest(
    "[data-action],[data-view],[data-mode],[data-candidate],[data-call]",
  );
  if (!el) return;
  try {
    if (el.dataset.view) {
      if (ui.dirty && !confirm("Leave without saving your edits?")) return;
      ui.view = el.dataset.view;
      ui.dirty = false;
      render();
      if (["sequence", "setup"].includes(ui.view)) await refresh(true);
      else if (["calls", "week", "scorecard", "events", "hiring", "starts", "month"].includes(ui.view) && !ui[ui.view]) await refresh(true); // the engine runs on Refresh
      await loadSourcing();
      return;
    }
    if (el.dataset.mode) {
      if (ui.dirty && !confirm("Switch workspaces without saving your edits?"))
        return;
      ui.mode = el.dataset.mode;
      localStorage.setItem("gi-mode", ui.mode);
      ui.selected = null;
      ui.state = null;
      ui.sequence = null;
      ui.sequenceDemo = null;
      ui.calls = null;
      ui.call = null;
      ui.sourcing = null;
      ui.trails = {};
      ui.sourcingError = "";
      ui.asks = {};
      ui.askDraft = {};
      ui.scorecard = null;
      ui.events = null;
      ui.week = ui.hiring = ui.starts = ui.month = null;
      ui.callsError = ui.scorecardError = ui.eventsError = ui.weekError = ui.hiringError = ui.startsError = ui.monthError = "";
      ui.dirty = false;
      render();
      await refresh(true);
      return;
    }
    if (el.dataset.call) {
      ui.call = el.dataset.call;
      render();
      await loadSourcing();
      return;
    }
    if (el.dataset.candidate) {
      ui.selected = el.dataset.candidate;
      render();
      return;
    }
    const a = el.dataset.action;
    if (a === "toggle-older") {  // the sidebar alone, so a form being filled in on the page keeps what was typed
      ui.olderOpen = !ui.olderOpen;
      $(".sidebar").outerHTML = nav();
      return;
    }
    if (a === "company-refresh") {
      el.disabled = true;
      try {
        await api("/api/company-context/refresh", "POST", {});
        toast("GI source refresh queued. Inspect Sources & runs for progress.");
        await refresh(true);
      } finally { el.disabled = false; }
      return;
    }
    if (["crustdata-connect", "crustdata-sync"].includes(a)) {
      if (ui.mode !== "live") throw new Error("Provider monitoring is available in the live workspace.");
      el.disabled = true;
      try {
        const result = await api(`/api/crustdata/${a === "crustdata-connect" ? "connect" : "sync"}`, "POST", {});
        ui.crustdataReport = result.note || result.message || (a === "crustdata-connect" ? "Provider connection queued. Inspect progress in Sources & runs; subscriptions appear after the job completes." : "Provider check queued. Inspect progress in Sources & runs.");
        toast(ui.crustdataReport);
        await refresh(true);
      } finally { el.disabled = false; }
      return;
    }
    if (a === "watch-enroll") { watchDialog(); return; }
    if (a === "role-drop") { roleDropDialog(); return; }
    if (a === "role-sample") {
      ui.roleDrop = {};
      rolePreviewDialog(await api("/api/roles/preview", "POST", { sample: true }));
      return;
    }
    if (a === "role-remove") {
      if (!confirm(`Remove ${el.dataset.title}? Its entry in config/roles.json and its spec go; anyone listed with it is left out of the next run.`)) return;
      await api(`/api/roles/${encodeURIComponent(el.dataset.role)}`, "DELETE", {});
      toast(`${el.dataset.title} removed.`);
      await refresh(true);
      return;
    }
    if (a === "sequence-person") {
      ui.selected = el.dataset.personId;
      ui.view = "watchlist";
      ui.role = "all";
      ui.query = "";
      render();
      return;
    }
    if (a === "sequence-demo" || a === "sequence-monitor") {
      el.disabled = true;
      try {
        if (a === "sequence-demo") {
          ui.sequenceDemo = await api("/api/sequence/demo", "POST", {});
          toast("Synthetic timing sequence completed. Inspect its observations and internal alerts below.");
        } else {
          await api("/api/settings", "POST", { monitoring_enabled: el.dataset.enabled === "true" });
          toast(el.dataset.enabled === "true" ? "Background monitoring enabled while the local server runs. Provider calls can incur charges." : "Background monitoring paused.");
        }
        await refresh(true);
      } finally { el.disabled = false; }
      return;
    }
    if (["sequence-run", "watch-check", "watch-toggle"].includes(a)) {
      el.disabled = true;
      try {
        const result = a === "sequence-run"
          ? await api("/api/sequence/run", "POST", { mode: ui.mode })
          : await api(`/api/watches/${encodeURIComponent(el.dataset.watchId)}${a === "watch-check" ? "/check" : ""}`, a === "watch-check" ? "POST" : "PATCH", a === "watch-check" ? {} : { status: el.dataset.status });
        toast(result.message || (a === "watch-toggle" ? "Watch updated." : "Source checks queued. This does not contact anyone."));
        await refresh(true);
      } finally { el.disabled = false; }
      return;
    }
    if (a === "close") {
      closeDialog();
      return;
    }
    if (a === "refresh") {
      await refresh(true);
      return;
    }
    if (a === "calls-refresh" || a === "week-refresh" || a === "scorecard-refresh" || a === "events-refresh" || a === "hiring-refresh" || a === "starts-refresh" || a === "month-refresh") {
      await refresh(true);
      return;
    }
    if (a === "event-invite" || a === "event-sent") {
      el.disabled = true;
      const invite = a === "event-invite";
      const by = invite ? el.closest(".evidence-item")?.querySelector("select[name=by]")?.value : list(ui.events?.last?.follow_ups).find((f) => f.subject_id === el.dataset.subject)?.host;
      const res = await api(`/api/events/${encodeURIComponent(el.dataset.event)}/ledger`, "POST", { mode: ui.mode, kind: invite ? "invited" : "sent", subject_id: el.dataset.subject, by: by || "", also_invite: invite ? null : el.dataset.invite || null });
      opsChanged(); // every ops page reads the same ledger
      toast(`${invite ? "Marked as invited" : el.dataset.invite ? "Marked as followed up and invited" : "Marked as followed up"}: nobody else at GI messages them for now. ${res.hold || ""}`);
      await refresh(true);
      return;
    }
    if (a === "hiring-add-person" || a === "hiring-add-seats" || a === "hiring-remove") {
      if (ui.hiringSaving) return;
      const data = ui.hiring, box = el.closest(".hiring-row"), form = document.querySelector(".hiring-form");
      if (ui.dirty && form && !form.reportValidity()) return; // unsaved assumptions go with the change, once they are valid
      const assumptions = ui.dirty && form ? hiringAssumptions(form) : data.assumptions;
      let lines = planLines(data);
      if (a === "hiring-remove") lines = lines.filter((_, i) => i !== Number(el.dataset.index));
      else {
        const p = a === "hiring-add-person" ? list(data.people).find((x) => x.subject_id === el.dataset.subject) : null;
        lines.push({ role_id: p ? p.role_id : el.dataset.role, subject_id: p ? p.subject_id : null, name: p ? p.name : null, office: box.querySelector("[name=office]")?.value || null, count: p ? 1 : Number(box.querySelector("[name=count]").value || 1), start_month: Number(box.querySelector("[name=start]").value), source: box.querySelector("[name=source]").value });
      }
      await savePlan(assumptions, lines);
      ui.dirty = false;
      return;
    }
    if (a === "month-copy") {
      await navigator.clipboard.writeText(ui.month?.text || "");
      toast("Copied as text.");
      return;
    }
    if (a === "month-print") {
      window.print();
      return;
    }
    if (a === "start-remove") {
      if (!confirm(`Remove ${el.dataset.name} from New starts? Their checklist and what they said at the offer stage go too.`)) return;
      el.disabled = true;
      await api(`/api/starts/${encodeURIComponent(el.dataset.hire)}`, "DELETE", { mode: ui.mode });
      opsChanged(); // a hire taken off before their start is a candidate again
      toast("Removed from New starts.");
      await refresh(true);
      return;
    }
    if (a === "copy-invite") {
      const p = list(ui.events?.next?.invite).find((x) => x.subject_id === el.dataset.subject);
      await navigator.clipboard.writeText([p?.draft?.subject, p?.draft?.body].filter(Boolean).join("\n\n"));
      toast("Draft copied.");
      return;
    }
    if (a === "copy-follow-up") {
      const f = list(ui.events?.last?.follow_ups).find((x) => x.subject_id === el.dataset.subject);
      await navigator.clipboard.writeText([f?.draft?.subject, f?.draft?.body].filter(Boolean).join("\n\n"));
      toast("Draft copied.");
      return;
    }
    if (a === "ask-suggested") {
      const label = SUGGESTED.find(([k]) => k === el.dataset.key)?.[1] || "";
      await askAbout(el.dataset.subject, { suggested: el.dataset.key }, label);
      return;
    }
    if (a === "board-open") { // the fixed preview of reading GI and Medal's job board: nothing to fetch
      if (ui.dirty && !confirm("Leave without saving your edits?")) return;
      closeDialog();
      ui.view = "board";
      render();
      return;
    }
    if (a === "behind-open") { // from a role just added: what the engine watches, on Today's calls
      closeDialog();
      [ui.view, ui.behind] = ["calls", true];
      render();
      if (!ui.calls) await refresh(true);
      await loadSourcing();
      return;
    }
    if (a === "behind-toggle") {
      ui.behind = !ui.behind;
      render();
      await loadSourcing();
      return;
    }
    if (a === "calls-back") {
      ui.call = null;
      render();
      return;
    }
    if (a === "copy-call-draft") {
      const call = list(ui.calls?.calls).find((x) => x.subject_id === ui.call) || list(ui.calls?.calls)[0];
      await navigator.clipboard.writeText([call?.draft?.subject, call?.draft?.body].filter(Boolean).join("\n\n"));
      toast("Draft copied.");
      return;
    }
    if (a === "back") {
      ui.selected = null;
      render();
      return;
    }
    if (a === "watchlist") {
      ui.view = "watchlist";
      render();
      return;
    }
    if (a === "add") {
      addPerson();
      return;
    }
    if (a === "edit-note") {
      const note = list(current()?.private_notes).find((n) => n.id === el.dataset.noteId);
      const form = $("#private-note-form");
      if (!note || !form) return;
      form.elements.text.value = note.quote;
      form.elements.author.value = note.author;
      form.elements.supersedes.value = note.id;
      form.elements.note_type.value = { note_open: "open", note_wait: "wait" }[note.event_type] || "";
      form.elements.wait_until.value = note.event_type === "note_wait" ? note.event_date : "";
      updateNoteWait(form);
      ui.dirty = true;
      form.elements.text.focus();
      return;
    }
    if (a === "add-evidence") {
      await refresh(true);
      evidenceDialog();
      return;
    }
    if (a === "edit-route") {
      await refresh(true);
      routeDialog();
      return;
    }
    if (a === "import-json") {
      closeDialog();
      editor();
      return;
    }
    if (a === "edit-draft") {
      await refresh(true);
      editDraft();
      return;
    }
    if (a === "edit") {
      await refresh(true);
      editor(current());
      return;
    }
    if (a === "copy") {
      const d = current()?.draft;
      if (!d?.body) throw new Error("There is no draft to copy yet.");
      await navigator.clipboard.writeText(
        `Subject: ${d.subject || ""}\n\n${d.body}`,
      );
      toast("Draft copied. Review the sender and evidence before sending.");
      return;
    }
    if (a === "aliases") {
      aliasDialog();
      return;
    }
    if (a === "distinct-person") {
      const other = el.dataset.other;
      openDialog(
        "Confirm different people",
        `<form id="distinct-form" data-other="${esc(other)}"><p>Describe the professional evidence that distinguishes these profiles. Matching names alone must not lead to a merge.</p><div class="form-group"><label for="distinct-note">Distinguishing evidence</label><textarea id="distinct-note" name="note" rows="4" required minlength="10"></textarea></div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Record distinct identities</button></div></form>`,
      );
      return;
    }
    if (a === "link-person") {
      if (
        !confirm(
          "Confirm these profiles belong to the same person? Their contact history and opt-out state will be combined.",
        )
      )
        return;
      await api("/api/people/link", "POST", {
        candidate_id: current().id,
        same_person_as: el.dataset.other,
      });
      opsChanged(); // one person's history now: every ops page reads it
      closeDialog();
      toast("Identities linked. Review the combined contact context.");
      await refresh(true);
      return;
    }
    if (a === "review") {
      await refresh(true);
      const c = current();
      openDialog(
        "Verify this opportunity",
        `<p>Open the linked sources and confirm the claims yourself. This records your review; it does not send outreach.</p><form id="review-form" data-id="${esc(c.id)}" data-revision="${c.revision}"><label class="check"><input type="checkbox" name="identity_confirmed" required><span>I confirmed the profile and evidence refer to this person.</span></label><label class="check"><input type="checkbox" name="evidence_confirmed" required><span>I verified the linked evidence, dates, timing claim, warm-route claims, and factual claims in the outreach draft.</span></label><label class="check"><input type="checkbox" name="fit_confirmed" required><span>I reviewed the role fit, missing or contradictory requirements, and the proposed sender and message.</span></label><div class="form-group"><label for="review-note">Review note</label><textarea id="review-note" name="note" rows="3" placeholder="What is convincing? What is still uncertain?"></textarea></div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Record review & evaluate</button></div></form>`,
      );
      return;
    }
    if (a === "snooze") {
      const next = new Date(Date.now() + 7 * 86400000)
        .toISOString()
        .slice(0, 10);
      openDialog(
        "Revisit at a better moment",
        `<form id="action-form" data-kind="snooze"><p>Keep the evidence and draft, and choose when this person should return for review.</p><div class="form-group"><label for="until">Revisit date</label><input id="until" name="until" type="date" value="${next}" required></div><div class="form-group"><label for="action-note">Reason</label><textarea id="action-note" name="note" rows="3" placeholder="For example, wait until the current project concludes."></textarea></div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary">Snooze person</button></div></form>`,
      );
      return;
    }
    if (a === "mark_sent") {
      openDialog(
        "Log outreach already sent",
        `<form id="action-form" data-kind="mark_sent"><p>This only records a message you sent outside this application. It updates contact history so the engine can avoid duplicate approaches.</p><label class="check"><input type="checkbox" required><span>${ui.mode === "simulation" ? "Record a synthetic outreach event for this simulation." : "I have already contacted this person outside this app."}</span></label><div class="form-group"><label for="action-note">Who sent it, where, and what was said?</label><textarea id="action-note" name="note" rows="3" required></textarea></div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary">Log outreach</button></div></form>`,
      );
      return;
    }
    if (a === "feedback") {
      openDialog(
        "Evaluate this opportunity",
        `<form id="feedback-form"><p>Record what worked or failed. Feedback is available for evaluation; it does not silently change hiring requirements or timing rules.</p><div class="form-group"><label for="feedback-label">Assessment</label><select id="feedback-label" name="label"><option value="useful">Useful opportunity</option><option value="wrong_fit">Wrong role fit</option><option value="wrong_time">Wrong moment</option><option value="wrong_identity">Wrong identity</option><option value="weak_route">Weak or unsupported route</option><option value="weak_draft">Weak outreach draft</option><option value="missed_signal">Missed a useful signal</option></select></div><div class="form-group"><label for="feedback-note">What should the engine learn from this?</label><textarea id="feedback-note" name="note" rows="4" required minlength="10" placeholder="Describe the evidence and the better decision."></textarea></div><div class="dialog-actions">${button("Cancel", "close")}<button class="button primary" type="submit">Save feedback</button></div></form>`,
      );
      return;
    }
    if (a === "more") {
      openDialog(
        "Manage this person",
        `<p>Dismiss removes the current opportunity from attention. Opt out prevents further outreach recommendations. Reopen requests another assessment.</p><div class="actions">${button("Dismiss opportunity", "dismiss")}${button("Record opt-out", "opt_out", "danger")}${button("Reopen for assessment", "reopen")}</div>`,
      );
      return;
    }
    if (["dismiss", "opt_out", "reopen"].includes(a)) {
      if (
        a === "opt_out" &&
        !confirm(
          "Record an opt-out for this person? Future outreach recommendations will be suppressed.",
        )
      )
        return;
      await candidateAction(a);
      return;
    }
    if (a === "reset-simulation") {
      if (
        !confirm(
          "Reset all synthetic candidates, decisions, and contact history? Live data will remain unchanged.",
        )
      )
        return;
      await api("/api/simulation/reset", "POST", {});
      ui.selected = null;
      ui.call = null;
      opsChanged(true); // the reset clears the demo store, plan and starts too
      toast("Simulation reset.");
      await refresh(true);
      return;
    }
    el.disabled = true;
    if (a === "evaluate-run") await run("evaluate");
    if (a === "discover") await run("discover", el.dataset.role);
    if (a === "research") await run("research");
  } catch (e) {
    toast(e.message, true);
  } finally {
    el.disabled = false;
  }
});
document.addEventListener("input", (ev) => {
  if (ev.target.id === "candidate-search") {
    const position = ev.target.selectionStart;
    ui.query = ev.target.value;
    render();
    const input = $("#candidate-search");
    input.focus();
    try {
      input.setSelectionRange(position, position);
    } catch {}
    return;
  }
  const ask = ev.target.closest(".ask-form");
  if (ask) ui.askDraft[ask.dataset.subject] = ev.target.value; // a question is not an unsaved edit
  else if (ev.target.closest("form")) ui.dirty = true;
});
document.addEventListener("dragover", (ev) => {
  if (ev.target.closest?.(".drop-zone")) ev.preventDefault();
});
document.addEventListener("drop", (ev) => {
  if (!ev.target.closest?.(".drop-zone")) return;
  ev.preventDefault();
  readDroppedFile(ev.dataTransfer.files[0]);
});
document.addEventListener("change", (ev) => {
  if (ev.target.id === "drop-file") {
    readDroppedFile(ev.target.files[0]);
    return;
  }
  if (ev.target.id === "include-event") {
    const fields = $("#event-fields");
    fields.hidden = !ev.target.checked;
    fields.disabled = !ev.target.checked;
    $("#evidence-date").required = ev.target.checked;
    updateTimingFields();
    return;
  }
  if (ev.target.id === "timing-action") {
    updateTimingFields();
    return;
  }
  if (ev.target.name === "note_type" && ev.target.form?.id === "private-note-form") {
    updateNoteWait(ev.target.form);
    return;
  }
  if (ev.target.form?.classList.contains("start-item")) {
    saveStartItem(ev.target.form).catch((e) => toast(e.message, true));
    return;
  }
  if (ev.target.id === "start-from-plan" || (ev.target.name === "role_id" && ev.target.form?.classList.contains("start-add"))) {
    const form = ev.target.form, p = list(ui.starts?.from_plan)[Number(ev.target.value)];
    if (ev.target.id === "start-from-plan" && p) {
      form.elements.name.value = p.name;
      form.elements.role_id.value = p.role_id;
      form.elements.start.value = p.start;
    }
    const role = list(ui.starts?.roles).find((r) => r.id === form.elements.role_id.value);
    form.elements.office.innerHTML = officeOptions(role?.offices);
    if (ev.target.id === "start-from-plan" && p?.office) form.elements.office.value = p.office;
    return;
  }
  if (ev.target.id === "role-filter") {
    ui.role = ev.target.value;
    ui.selected = null;
    render();
  }
});
document.addEventListener("submit", async (ev) => {
  ev.preventDefault();
  if (ev.target.classList.contains("ask-form")) {  // an answer, not a save: nothing else on the page reloads
    const question = String(new FormData(ev.target).get("question") || "").trim();
    ui.askDraft[ev.target.dataset.subject] = "";
    if (question) await askAbout(ev.target.dataset.subject, { question }, question);
    return;
  }
  const form = ev.target,
    submit =
      form.querySelector('button[type="submit"]') ||
      form.querySelector("button.primary");
  if (submit) submit.disabled = true;
  const fd = new FormData(form);
  try {
    if (form.id === "private-note-form") {
      await api(`/api/people/${encodeURIComponent(form.dataset.personId)}/notes`, "POST", { text: String(fd.get("text")).trim(), author: String(fd.get("author")).trim(), supersedes: String(fd.get("supersedes") || "") || null, note_type: String(fd.get("note_type") || "") || null, wait_until: String(fd.get("wait_until") || "") || null });
      toast("Private note saved.");
    } else if (form.id === "watch-form") {
      const urls = String(fd.get("urls") || "").split("\n").map((value) => value.trim()).filter(Boolean);
      if (urls.length > 8) throw new Error("Choose up to eight sources for this watch.");
      if (urls.some((value) => url(value) === "#")) throw new Error("Use full HTTP or HTTPS source URLs, one per line.");
      await api("/api/watches", "POST", { candidate_id: form.dataset.personId, fit_basis: String(fd.get("fit_basis")).trim(), organization: String(fd.get("organization") || "").trim(), urls });
      closeDialog();
      ui.view = "sequence";
      toast("Watch enrolled. Check its sources to establish a baseline.");
    } else if (form.id === "role-drop-form") {
      ui.roleDrop = { jd_url: String(fd.get("jd_url") || "").trim(), locations: String(fd.get("locations") || "").split(",").map((o) => o.trim()).filter(Boolean) };
      const label = submit.textContent;
      submit.textContent = "Reading the JD: two model calls…"; // in the box: a toast would sit behind it
      let previewed;
      try {
        previewed = await api("/api/roles/preview", "POST", { jd_text: String(fd.get("jd_text")).trim(), jd_url: ui.roleDrop.jd_url });
      } finally {
        submit.textContent = label;
      }
      rolePreviewDialog(previewed);
      return;
    } else if (form.id === "role-accept-form") {
      const role = await api("/api/roles/accept", "POST", { preview: ui.rolePreview, ...ui.roleDrop });
      ui.rolePreview = null;
      ui.view = "roles";
      openDialog(`${esc(role.title)} added`, `<ul>${list(role.next).map((line) => `<li>${esc(line)}</li>`).join("")}</ul><div class="dialog-actions">${button("Close", "close")}${button("New job posts", "board-open")}${button("Open Behind the scenes", "behind-open", "primary")}</div>`);
    } else if (form.id === "route-form") {
      const c = structuredClone(ui.editCandidate || current());
      delete c.decision;
      c.route = {
        status: fd.get("status"),
        description:
          String(fd.get("description")).trim() || "No warm path found.",
        introducer: String(fd.get("introducer")).trim(),
        evidence_ids: fd.getAll("route_sources"),
      };
      if (c.route.status !== "none_found" && !c.route.evidence_ids.length)
        throw new Error(
          "Add and select evidence for this proposed relationship first.",
        );
      await api(`/api/candidates/${encodeURIComponent(c.id)}`, "PUT", c);
      closeDialog();
      toast(
        "Route saved. Confirm relationship evidence before requesting an introduction.",
      );
    } else if (form.id === "feedback-form") {
      await api(
        `/api/candidates/${encodeURIComponent(current().id)}/feedback`,
        "POST",
        { label: fd.get("label"), note: fd.get("note") },
      );
      closeDialog();
      toast("Feedback saved for evaluation.");
    } else if (form.id === "evidence-form") {
      const c = structuredClone(ui.editCandidate || current());
      delete c.decision;
      const id = "evidence:" + crypto.randomUUID();
      const observed = new Date().toISOString();
      c.evidence.push({
        id,
        url: String(fd.get("url")).trim(),
        title: String(fd.get("title")).trim(),
        excerpt: String(fd.get("excerpt")).trim(),
        observed_at: observed,
        event_date: fd.get("event_date") || null,
        verified: false,
        source_kind: "manual_source",
      });
      for (const criterion of fd.getAll("supports")) {
        const f = c.fit.find((x) => x.criterion === criterion);
        if (f) {
          f.status = "supported";
          f.evidence_ids = [...new Set([...f.evidence_ids, id])];
        } else
          c.fit.push({ criterion, status: "supported", evidence_ids: [id] });
      }
      if (fd.has("include_event")) {
        if (!fd.get("event_date"))
          throw new Error("A timing event needs a supported event date.");
        const timingAction = fd.get("timing_action") || "watch";
        const recipientValue = String(fd.get("recipient_value") || "").trim();
        const followUpOn = timingAction === "follow_up" ? fd.get("follow_up_on") : null;
        const followUpQuote = timingAction === "follow_up" ? String(fd.get("follow_up_quote") || "").trim() : "";
        if (timingAction === "contact_now" && recipientValue.length < 15)
          throw new Error("Explain the concrete value to this person before proposing contact now.");
        if (timingAction === "follow_up" && (!followUpOn || !followUpQuote))
          throw new Error("A follow-up date needs an explicit sourced request or constraint.");
        if (followUpQuote && !String(fd.get("excerpt")).includes(followUpQuote))
          throw new Error("The follow-up quote must appear exactly in the source excerpt.");
        c.events.push({
          id: "event:" + crypto.randomUUID(),
          kind: fd.get("kind"),
          title: String(fd.get("event_title")).trim(),
          date: fd.get("event_date"),
          timing_action: timingAction,
          recipient_value: recipientValue,
          follow_up_on: followUpOn,
          follow_up_quote: followUpQuote,
          why_now: String(fd.get("why_now")).trim(),
          why_wait: String(fd.get("why_wait")).trim(),
          falsifier: String(fd.get("falsifier")).trim(),
          evidence_ids: [id],
          conversation_score: Number(fd.get("conversation_score")),
          career_openness: fd.get("career_openness"),
          confidence: fd.get("confidence"),
        });
      }
      await api(`/api/candidates/${encodeURIComponent(c.id)}`, "PUT", c);
      closeDialog();
      toast("Evidence added. Review the source before releasing a ping.");
    } else if (form.id === "distinct-form") {
      await api("/api/people/distinct", "POST", {
        candidate_id: current().id,
        other_candidate_id: form.dataset.other,
        note: fd.get("note"),
      });
      opsChanged();
      closeDialog();
      toast("Different identities recorded.");
    } else if (form.id === "add-person-form") {
      const data = {
        name: String(fd.get("name")).trim(),
        profile_url: String(fd.get("profile_url")).trim(),
        role_id: fd.get("role_id"),
        mode: ui.mode,
        summary: String(fd.get("summary") || "").trim(),
      };
      const res = await api("/api/candidates", "POST", data);
      ui.selected = res.id || res.candidate?.id || null;
      ui.view = "watchlist";
      closeDialog();
      toast("Person added. Research updates to build their evidence record.");
    } else if (form.id === "draft-form") {
      const c = structuredClone(ui.editCandidate || current());
      delete c.decision;
      c.draft = {
        ...c.draft,
        sender_function: fd.get("sender_function"),
        sender_name: fd.get("sender_name"),
        subject: fd.get("subject"),
        body: fd.get("body"),
        channel: fd.get("channel"),
        source_ids: fd.getAll("draft_sources"),
      };
      await api(`/api/candidates/${encodeURIComponent(c.id)}`, "PUT", c);
      closeDialog();
      toast("Draft saved. Review this revised approach before using it.");
    } else if (form.id === "candidate-form") {
      const data = JSON.parse($("#candidate-json").value);
      delete data.decision;
      const path = form.dataset.id
        ? `/api/candidates/${encodeURIComponent(form.dataset.id)}`
        : "/api/candidates";
      const res = await api(path, form.dataset.id ? "PUT" : "POST", data);
      ui.selected = res.id || res.candidate?.id || ui.selected;
      closeDialog();
      toast("Research saved. Earlier review has been invalidated.");
    } else if (form.id === "review-form") {
      await api(
        `/api/candidates/${encodeURIComponent(form.dataset.id)}/review`,
        "POST",
        {
          revision: Number(form.dataset.revision),
          note: fd.get("note") || "",
          identity_confirmed: fd.has("identity_confirmed"),
          evidence_confirmed: fd.has("evidence_confirmed"),
          fit_confirmed: fd.has("fit_confirmed"),
        },
      );
      closeDialog();
      toast("Review recorded.");
    } else if (form.id === "action-form") {
      await candidateAction(form.dataset.kind, {
        note: fd.get("note") || "",
        ...(fd.get("until")
          ? { until: new Date(fd.get("until") + "T12:00:00").toISOString() }
          : {}),
      });
      return;
    } else if (form.classList.contains("door-note")) {
      const kind = String(fd.get("kind"));
      if (kind === "wait" && !fd.get("wait_until")) throw new Error("Pick the day to check back on.");
      const res = await api(`/api/events/${encodeURIComponent(form.dataset.event)}/notes`, "POST", { mode: ui.mode, subject_id: form.dataset.subject, kind, text: String(fd.get("text")).trim(), author: String(fd.get("author") || ""), wait_until: kind === "wait" ? String(fd.get("wait_until")) : null });
      opsChanged(); // every ops page reads the same timeline
      toast(`Saved. Their call on Today's calls is now: ${res.call}.${res.holding.length ? ` Still holding: ${res.holding.join("; ")}.` : ""}`);
    } else if (form.classList.contains("start-item")) {
      return; // Enter in the note field: its change event already saved it
    } else if (form.classList.contains("start-add")) {
      const pick = list(ui.starts?.from_plan)[Number($("#start-from-plan")?.value ?? -1)];
      const name = String(fd.get("name")).trim();
      await api("/api/starts", "POST", { mode: ui.mode, name, role_id: String(fd.get("role_id")), office: String(fd.get("office")), start: String(fd.get("start")), offer_on: String(fd.get("offer_on")), subject_id: pick && pick.name === name ? pick.subject_id : null });
      ui.dirty = false;
      opsChanged(); // someone who accepted an offer is no longer a candidate anywhere
      toast(`${name} added to New starts.`);
      await refresh(true);
      return;
    } else if (form.classList.contains("hiring-form")) {
      if (ui.hiringSaving) return;
      await savePlan(hiringAssumptions(form), planLines(ui.hiring));
      ui.dirty = false;
      toast("Plan updated.");
      return;
    } else if (form.classList.contains("role-form")) {
      await api(
        `/api/roles/${encodeURIComponent(form.dataset.roleId)}`,
        "PUT",
        {
          brief_version: Number(form.dataset.version),
          criteria: String(fd.get("criteria"))
            .split("\n")
            .map((x) => x.trim())
            .filter(Boolean),
          manager_notes: fd.get("manager_notes") || "",
        },
      );
      toast("Role brief saved. Existing approvals require review.");
    } else if (form.id === "settings-form") {
      const data = {
        model: String(fd.get("model")).trim(),
        max_calls_per_run: Number(fd.get("max_calls_per_run")),
        monitoring_enabled: fd.has("monitoring_enabled"),
      };
      for (const key of [
        "exa_key",
        "crustdata_key",
        "anthropic_key",
        "anthropic_workspace_id",
      ])
        if (String(fd.get(key) || "").trim())
          data[key] = String(fd.get(key)).trim();
      await api("/api/settings", "POST", data);
      toast("Settings saved.");
    }
    ui.dirty = false;
    await refresh(true);
  } catch (e) {
    if ($("#dialog").open) {
      $("#dialog-error").textContent = e.message;
      $("#dialog-error").scrollIntoView({ block: "nearest" }); // below a box's buttons, which stay in view
    } else toast(e.message, true);
  } finally {
    if (submit) submit.disabled = false;
  }
});
$("#dialog").addEventListener("cancel", () => {
  ui.dirty = false;
});
window.addEventListener("beforeunload", (e) => {
  if (ui.dirty) {
    e.preventDefault();
    e.returnValue = "";
  }
});
render();
refresh();
setInterval(() => {
  if (!document.hidden) refresh();
}, 5000);
