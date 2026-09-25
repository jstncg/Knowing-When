// End to end: Today's calls' "Behind the scenes" view and "Ask about this person" panel, driven in a real browser
// against the real app, on invented people only (the simulation, and the same people in a throwaway live workspace with an invented daily-run log).
// The live half's timelines, pull folder and app store are temp files; it still reads the repo's research/private
// team, contacts and profile files if present, which name none of the invented people. Nothing is written outside a
// temp folder, and nothing spends: the one free-text question gets a canned answer in the browser, and the server
// holds a made-up model key, so a question that got past that would be refused unbilled (the model path itself is
// tests/test_ask.py's).
//
//   node tests/e2e/todays_calls.cjs [out-dir]      (default: a new folder under the system temp dir)
//
// Needs uv and Playwright with Chromium (npm i -g playwright; npx playwright install chromium). The artifact is the
// out-dir: screenshots of each view, transcript.json (what each panel said) and checks.txt (each check below, passed).
//
// Ways it could fail, written before the code they check (Justin's rule, 2026-09-24):
//  1. The toggle shows nothing or sticks on "Reading…": an API error, a person with no trail, or the toggle pressed
//     while the calls are still loading.
//  2. A source is named by the tool that pulls it (Apify, Crustdata, Exa...) anywhere on the page.
//  3. The trail's trigger is not the item part 3 (Trigger) links to.
//  4. Time in a seat counts toward a call, or is listed while the engine doesn't list it (an anniversary months away).
//  5. A hold is shown that isn't holding the call now (not started yet, or over), or a day shown as "open until" or
//     "until" is already past.
//  6. The trail says what a draft is about, or names a draft for a call that has none.
//  7. A post that touches something personal is quoted in the trail.
//  8. Live: no count of what the last pull stored, runs not newest first, a run that posted nothing reads "Posted",
//     or items without the time they were pulled.
//  9. Switching workspace leaves the other workspace's panel on screen.
//  9b. Refresh leaves the trail as it was before the calls reran.
// 10. "undefined" or "NaN" on screen, a page error, or an unknown person or workspace answered with anything but a
//     404 or a 422.
// 11. At phone width the page scrolls sideways.
// 12. A suggested answer cites nothing, or an item the trail doesn't show, or a cite links nowhere.
// 13. Asking a question reruns the engine for the whole page.
// 14. Typing a question counts as an unsaved edit, so switching workspace asks to discard it and stays put; the other
//     workspace's answers stay on screen after the switch; an answer arriving wipes a question half typed.
// 15. A model answer doesn't say what it cost, or a free one reads as paid.
// Written after the Mac's check on real data (2026-09-24), before the code that answers them:
// 16. The words of a post the reader read nothing off (a curse, politics, a casual reply) are shown anywhere: the
//     trail, or any answer. Only items read or counted are shown; the rest are a count.
// 17. "Match to the job description" rests on single common words, or on a post the reader read nothing off.
// 18. "What they shipped lately" lists plain posts: anything but releases, papers and launches.
// 19. Developer words on the page: a store's path, raw type names ("github activity", "profile job started"), role
//     ids ("(product-designer)").
// 20. An item's lines disagree: a window "seen" or "closed" beside one that counts, or "counts" beside "On the call:
//     No".
// Found in review:
// 21. The call's own signals (part 3, "Every signal behind the call") quote a post the reader read nothing off, which
//     a burst of posting counts; a launch-week hold on a launch already out reads as a launch still to come.
// Written after the Mac's second check on real data (2026-09-24), before the code that answers them:
// 22. An item quotes the post it replies to or quotes ("In reply to @someone: ..."), on the page or in an answer:
//     only the person's own words are theirs to show.
// 23. The job description answer's count leaves out a kind of item its matches cite (a release, a launch, a paper).
// 24. The team answer says one shared topic more than once or names a teammate twice (tests/test_ask.py: the
//     browser check has no team file).
// 25. An item shown as a public moment has nothing under The engine and "No" on the call, with no word why.
// Found in review, before the code that answers them:
// 26. An older release says the call counts a newer one when that newer one only holds the call back or waits for
//     it, or is someone else's; or the note names no day, or the wrong one.
// 27. "What would prove it wrong" (part 5) quotes the post they replied to (tests/test_ask.py 21: the browser check's
//     people have no role claims).
// 28. A post's shown words are more or less than their own: the reply cut too early, or their own words lost.
// 29. Behind the scenes hides the line of a GitHub release no one read, which the cards and Today's calls show
//     (detectors.unread): a release is their own work.
// Found in the Mac's check of 243a7b1, before the code that answers it:
// 30. The job description count's kinds add up to more than its total without saying some items count under more
//     than one kind, or say so when they don't.
const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const OUT = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), "gi-e2e-")));
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "gi-e2e-work-"));
const PORT = 8790 + Math.floor(Math.random() * 100);
const BASE = `http://127.0.0.1:${PORT}`;
const TOOLS = /\b(apify|apidojo|harvestapi|crustdata|exa|phantombuster)\b/i;
const PERSONAL = "Back at my desk after surgery last month, and back to training world models.";
// Invented stand-ins for what the Mac found on real data: posts the reader read nothing off (16), and a casual reply
// whose words a job description topic happens to use (17).
const UNTAGGED = ["is there any online shopping site that isn't fucking awful",
  "Another culture-war take on the election, and yes I stand by every word of it.",
  "@pixelfriend god bring that old app icon back"];
// Stand-in for a reply's context: the post they answered, which is never theirs to show (22).
const ANSWERED = "Our quarterly update on the harbour ferry timetable.";
const DEV_WORDS = /sqlite|research\/private|github activity|profile job|\((?:mts|mts-research|backend|product-designer|controller)\)/i;

function playwright() {
  try {
    return require("playwright");
  } catch {
    return require(path.join(execFileSync("npm", ["root", "-g"]).toString().trim(), "playwright"));
  }
}

// A live workspace of the invented people: their fixtures in a store of their own, one more invented person (Tamsin)
// whose windows sit either side of today (an equity grant's hold over, a conference deadline's still to come, a
// paper's reason closed but fading), and two daily runs in its log with route.py's own lines for what they posted;
// then, after the last run, one more post of Mara's that touches something personal: the one item that run stored.
// Before the runs, the stand-ins: Mara's untagged curse and politics and a tagged reply to someone else's post;
// Priya's casual reply and her profile's job start; Theo's GitHub release (stored twice, as a pull can, and the
// release the post reader read off it, in words a backend topic uses), one before it the reader read with no date,
// an older one whose month is over, and one no one read; Lena's launch three days ago (its launch week); Rowan (one
// more invented person), whose release the reader gave no date comes before a release still to come; and a card
// already sent to the designer who is to be reached (their id written to pinged.txt), so their way in says it is held.
function seed() {
  const env = { ...process.env, ANTHROPIC_API_KEY: "e2e-made-up-key", TIMELINES_DB: path.join(TMP, "timelines.sqlite"), SOCIAL_DIR: TMP, DATABASE_URL: `sqlite:///${path.join(TMP, "pilot.sqlite")}` };
  execFileSync("uv", ["run", "python", "-c", `
import json
from datetime import timedelta
from app import routing, today
from app.models import iso, utcnow
store = today.ledger()
for folder in today.DEMO:
    routing.seed(store, json.loads((folder / "timeline.json").read_text()))
now = utcnow()
def ev(t, days, quote, url):
    d = now + timedelta(days=days)
    return {"subject_id": "E900", "event_type": t, "event_date": d.date().isoformat(), "observed_at": iso(min(d, now) - timedelta(hours=1)), "quote": quote, "source_url": url}
routing.seed(store, {"contexts": [{"subject_id": "E900", "name": "Tamsin Vale", "role": "mts", "affiliations": ["Kestrel Labs"], "anchors": {"x": "https://x.com/tamsinvale_example"}}],
                     "events": [ev("equity_refresh", -210, "Grateful for the refresh grant this cycle.", "https://x.com/tamsinvale_example/status/1"),
                                ev("conference_deadline", 80, "Our workshop deadline is set.", "https://x.com/tamsinvale_example/status/2"),
                                ev("paper_accepted", -65, "Our paper on world models from gameplay video was accepted.", "https://openreview.net/forum?id=tv_example")]})
def post(sid, t, days, quote, url, **more):
    d = now - timedelta(days=days)
    return {"subject_id": sid, "event_type": t, "event_date": d.date().isoformat(), "observed_at": iso(d), "quote": quote, "source_url": url, **more}
release = "https://github.com/theomarsh-example/tanglewood/releases/tag/v1.0"
untagged = ${JSON.stringify(UNTAGGED)}
routing.seed(store, {"events": [post("X900", "x_post", 5, untagged[0], "https://x.com/maraquill_example/status/1010"),
                                post("X900", "x_post", 4, untagged[1], "https://x.com/maraquill_example/status/1011"),
                                post("D903", "linkedin_comment", 6, untagged[2], "https://www.linkedin.com/feed/update/urn:li:activity:9003010"),
                                post("D901", "github_activity", 12, "Released v1.0 of theomarsh-example/tanglewood: Tanglewood 1.0, object storage for video clips", release),
                                post("D901", "github_activity", 20, "Released v0.9.5 of theomarsh-example/tanglewood: Tanglewood 0.9.5", release.replace("v1.0", "v0.9.5")),
                                post("X900", "x_post", 6, "Our world models now train on twice the gameplay hours." + routing.REPLY_CONTEXT + "@ferrywatch_example: " + ${JSON.stringify(ANSWERED)}, "https://x.com/maraquill_example/status/1012"),
                                post("X900", "gi_topic", 6, "world models", "https://x.com/maraquill_example/status/1012", extractor="claude_extract_v1:posts:e2e"),
                                post("D901", "github_activity", 12, "Released v1.0 of theomarsh-example/tanglewood: Tanglewood 1.0, object storage for video clips (notes edited)", release),
                                post("D901", "project_release", 12, "Tanglewood 1.0 is out: clip transcoding for the upload pipeline.", release, extractor="claude_extract_v1:posts:e2e"),
                                post("D901", "github_activity", 45, "Released v0.9 of theomarsh-example/tanglewood: Tanglewood 0.9", release.replace("v1.0", "v0.9")),
                                post("D901", "project_release", 45, "Tanglewood 0.9, the first public cut.", release.replace("v1.0", "v0.9"), extractor="claude_extract_v1:posts:e2e"),
                                post("D901", "github_activity", 70, "Released v0.8 of theomarsh-example/tanglewood: Tanglewood 0.8, a preview", release.replace("v1.0", "v0.8")),
                                post("D904", "x_post", 3, "Kestrel Sim is out today: a simulator for training game agents on gameplay video.", "https://x.com/lenaokafor_example/status/9001"),
                                post("D904", "launch_announced", 3, "Kestrel Sim", "https://x.com/lenaokafor_example/status/9001", extractor="claude_extract_v1:posts:e2e"),
                                post("D903", "profile_job_started", 3, "Senior Product Designer at Loomwork since 2024-03", "https://www.linkedin.com/in/priyavance-example", event_date="2024-03", extractor="linkedin_profile_v1")]})
# the reader's release on v0.9.5 gives no date: a public moment (the day it went public) that no window opens on
from app import contact, timeline
timeline.add_event(store, {"subject_type": "person", "subject_id": "D901", "event_type": "project_release", "tier": 1, "observed_at": iso(now - timedelta(days=20)), "quote": "Tanglewood 0.9.5", "source_url": release.replace("v1.0", "v0.9.5"), "source_version_hash": "fixture", "extractor": "claude_extract_v1:posts:e2e"})
# Rowan: a release the reader gave no date, and a newer release still to come, which holds the call and counts nothing
grid = "https://github.com/rowanhale-example/gridworld/releases/tag/v0.3"
routing.seed(store, {"contexts": [{"subject_id": "E901", "name": "Rowan Hale", "role": "backend", "affiliations": ["Kestrel Labs"], "anchors": {"github": "https://github.com/rowanhale-example"}}],
                     "events": [post("E901", "github_activity", 20, "Released v0.3 of rowanhale-example/gridworld: Gridworld 0.3", grid),
                                post("E901", "x_post", 2, "Gridworld 1.0 ships next week.", "https://x.com/rowanhale_example/status/1"),
                                post("E901", "project_release", 2, "Gridworld 1.0", "https://x.com/rowanhale_example/status/1", event_date=(now + timedelta(days=8)).date().isoformat(), extractor="claude_extract_v1:posts:e2e")]})
timeline.add_event(store, {"subject_type": "person", "subject_id": "E901", "event_type": "project_release", "tier": 1, "observed_at": iso(now - timedelta(days=20)), "quote": "Gridworld 0.3", "source_url": grid, "source_version_hash": "fixture", "extractor": "claude_extract_v1:posts:e2e"})
reach = next(c for c in today.calls("live")["calls"] if c["action"] == "reach_now" and c["route"] and c["role"]["id"] == "product-designer")
contact.record(store, reach["subject_id"], "pinged", at=iso(now - timedelta(days=2)), role_id=reach["role"]["id"])
(today.TIMELINES.parent / "pinged.txt").write_text(reach["subject_id"])
now = utcnow()  # the last run starts after all of that is stored
runs = [{"at": iso(now - timedelta(days=1, hours=1)), "status": "ok", "pulled": (now - timedelta(days=1)).date().isoformat(), "new_events": 3, "read_calls": 1, "usd": 0.04, "slack": "Nothing new to reach: nothing posted."},
        {"at": iso(now), "status": "ok", "pulled": now.date().isoformat(), "new_events": 46, "read_calls": 2, "usd": 0.07, "slack": "Posted the morning list with 1 card in its thread"}]
today.DAILY_LOG.write_text("".join(json.dumps(r) + "\\n" for r in runs))
routing.seed(store, {"events": [post("X900", "x_post", 3, ${JSON.stringify(PERSONAL)}, "https://x.com/maraquill_example/status/1009"),
                                post("X900", "gi_topic", 3, "world models", "https://x.com/maraquill_example/status/1009", extractor="claude_extract_v1:posts:e2e")]})
`], { cwd: ROOT, env, stdio: "inherit" });
  return env;
}

async function up(env) {
  const server = spawn("uv", ["run", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", String(PORT)], { cwd: ROOT, env, stdio: ["ignore", "ignore", "pipe"] });
  let log = "";
  server.stderr.on("data", (d) => (log += d));
  for (let i = 0; i < 120; i++) {
    try {
      if ((await fetch(`${BASE}/api/state?mode=simulation`)).ok) return server;
    } catch {}
    await new Promise((r) => setTimeout(r, 500));
  }
  server.kill();
  throw new Error(`The app did not start:\n${log}`);
}

const checks = [], transcript = {};
function check(n, what, ok, detail = "") {
  checks.push(`${ok ? "PASS" : "FAIL"} ${n}. ${what}${detail ? ` (${detail})` : ""}`);
  if (!ok) throw new Error(`Check ${n} failed: ${what} ${detail}`);
}
const MONTHS = { Jan: 0, Feb: 1, Mar: 2, Apr: 3, May: 4, Jun: 5, Jul: 6, Aug: 7, Sep: 8, Oct: 9, Nov: 10, Dec: 11 };
const dayOf = (s) => { const m = /([A-Z][a-z]{2}) (\d{1,2}), (\d{4})/.exec(s); return m ? Date.UTC(+m[3], MONTHS[m[1]], +m[2]) : null; };

// One person's trail as the page shows it: its heading, and each item's four steps.
async function trailOf(page) {
  return page.$eval(".behind-trail", (t) => ({
    text: t.innerText,
    held: t.querySelector("[data-holds]")?.dataset.holds || "",
    hidden: Number(t.querySelector("[data-hidden]")?.dataset.hidden || 0),
    items: [...t.querySelectorAll(".trail-item")].map((i) => ({ text: i.innerText, link: i.querySelector("a")?.href || null, engine: [...i.querySelectorAll(".reason-list li")].map((l) => ({ state: l.className.replace("engine-", ""), text: l.innerText })), call: i.querySelector("[data-on-call]")?.dataset.onCall || "", read: Number(i.dataset.read), is: i.dataset.is, quote: i.querySelector("blockquote")?.innerText || "" })),
  }));
}

// Every person's trail in the workspace on screen, against the call the page shows for them (3 to 6).
async function checkTrails(page, where) {
  const asOf = dayOf(await page.$eval(".queue-heading", (e) => e.innerText));
  const people = await page.$$eval("[data-call]", (els) => els.map((e) => e.dataset.call));
  const trails = {};
  for (const who of people) {
    await page.click(`[data-call="${who}"]`);
    await page.waitForFunction((w) => document.querySelector(`[data-call="${w}"]`)?.classList.contains("selected") && document.querySelector(".behind-trail .trail"), who);
    const t = await trailOf(page);
    trails[who] = t;
    const trigger = t.items.filter((i) => i.call === "trigger");
    const part3 = await page.$$eval(".ping-part", (parts) => parts.find((p) => p.innerText.includes("Trigger"))?.querySelector("a")?.href || null);
    check(3, `${where} ${who}: the trail's trigger is part 3's item`, trigger.length === (part3 ? 1 : 0) && (!part3 || trigger[0].link === part3), `${trigger[0]?.link || "none"} / ${part3 || "none"}`);
    const lines = t.items.flatMap((i) => i.engine);
    const seat = lines.filter((l) => /work anniversary|retention/.test(l.text));
    check(4, `${where} ${who}: time in a seat never counts`, seat.every((l) => l.state === "context"), seat.map((l) => l.text).join(" | "));
    const holds = lines.filter((l) => l.state === "hold");
    check(5, `${where} ${who}: every hold shown is one the call has`, holds.every((l) => t.held.split("|").some((h) => h && l.text.includes(h))), `${holds.map((l) => l.text).join(" | ") || "no holds"} / ${t.held || "none held"}`);
    const past = lines.filter((l) => ["counts", "hold"].includes(l.state) && /until/.test(l.text) && dayOf(l.text.split("until")[1]) < asOf);
    check(5, `${where} ${who}: no "until" day already past`, past.length === 0, past.map((l) => l.text).join(" | "));
    check(6, `${where} ${who}: the trail never says what a draft is about`, !/draft/i.test(t.text));
    const detail = (t.detail = await page.$eval(".detail", (e) => e.innerText));
    check(21, `${where} ${who}: no untagged post's words anywhere on their page`, UNTAGGED.every((q) => !detail.includes(q)) && !/fucking|culture-war|app icon/i.test(detail));
    check(22, `${where} ${who}: nothing on their page quotes the post they replied to`, !detail.includes(ANSWERED) && !/In reply to/.test(detail));
    const mute = t.items.filter((i) => /Public moment:/.test(i.text) && !i.engine.length && !i.call);
    check(25, `${where} ${who}: every public moment says what the engine makes of it`, mute.length === 0, mute.map((i) => i.link).join(" | "));
    const bare = t.items.filter((i) => !i.read && !i.is && i.quote && !/^\(not quoted/.test(i.quote));
    check(16, `${where} ${who}: no post the reader read nothing off is quoted`, bare.length === 0, bare.map((i) => i.link).join(" | "));
    check(19, `${where} ${who}: no developer words in the trail`, !DEV_WORDS.test(t.text), (DEV_WORDS.exec(t.text) || [""])[0]);
    const WEAK = ["closed", "seen"];
    const mixed = t.items.filter((i) => i.engine.some((l) => WEAK.includes(l.state)) && i.engine.some((l) => !WEAK.includes(l.state)));
    check(20, `${where} ${who}: no item reads "seen" or "closed" beside a line that counts, holds or waits`, mixed.length === 0, mixed.map((i) => i.engine.map((l) => l.text).join(" + ")).join(" | "));
    const offCall = t.items.filter((i) => i.engine.some((l) => ["counts", "fading", "hold", "waits"].includes(l.state)) && !i.call);
    check(20, `${where} ${who}: an item that counts, holds or makes the call wait is on the call`, offCall.length === 0, offCall.map((i) => i.engine.map((l) => l.text).join(" + ")).join(" | "));
    const ahead = t.items.flatMap((i) => i.engine).filter((l) => /next few weeks/.test(l.text) && !["hold", "later", "waits"].includes(l.state));
    check(20, `${where} ${who}: nothing past reads as a launch still to come`, ahead.length === 0, ahead.map((l) => l.text).join(" | "));
  }
  return trails;
}

// The four suggested questions (or some) about one person, newest answer first as the page shows them.
async function askAll(page, who, keys) {
  await page.click(`[data-call="${who}"]`);
  await page.waitForFunction((w) => document.querySelector(`[data-call="${w}"]`)?.classList.contains("selected") && document.querySelector(".behind-trail .trail"), who);
  const out = {};
  for (const key of keys) {
    const n = await page.$$eval(".answer .reason-list", (a) => a.length);
    await page.click(`[data-action=ask-suggested][data-key=${key}]`);
    await page.waitForFunction((n) => document.querySelectorAll(".answer .reason-list").length === n + 1, n);
    out[key] = await page.$eval(".answer", (a) => ({ text: a.innerText, lines: [...a.querySelectorAll(".reason-list li")].map((l) => l.innerText), cites: [...a.querySelectorAll("a.cite")].map((c) => c.href) }));
  }
  out.trailLinks = await page.$$eval(".trail-item a", (as) => as.map((a) => a.href));
  return out;
}

async function run(page) {
  const errors = [];
  let trailReads = 0, callsRuns = 0, canned = 0;
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("request", (r) => r.url().includes("/trail?") && trailReads++);
  page.on("request", (r) => new URL(r.url()).pathname === "/api/calls" && callsRuns++);
  // The free-text question never reaches the model: a canned answer in the model's shape stands in for it.
  await page.route("**/api/calls/*/ask", async (route) => {
    const body = route.request().postDataJSON();
    if (!body.question) return route.continue();
    canned++;
    route.fulfill({ json: { question: body.question, lines: [{ text: "They asked in public for action-labeled gameplay video.", cites: [1] }], sources: [{ n: 1, label: "Public posts on X", date: "2026-09-08", url: "https://x.com/maraquill_example/status/1002" }], free: false, model: "claude-opus-5", usd: 0.021 } });
  });

  // The toggle pressed while the calls are still running ends on the panel and a trail (1): back on Today's calls
  // after a change on another page (opsChanged, as a door note on Events does), the calls held back until it is.
  await page.goto(BASE);
  await page.evaluate(() => localStorage.setItem("gi-mode", "simulation"));
  await page.goto(BASE);
  await page.waitForSelector("[data-call]");
  await page.click('[data-view="week"]');
  await page.waitForFunction(() => ui.week || ui.weekError);
  await page.evaluate(() => opsChanged());
  let release;
  const running = new Promise((r) => (release = r));
  const calls = (u) => u.pathname === "/api/calls";
  await page.route(calls, async (route) => { await running; await route.continue(); });
  await page.click('[data-view="calls"]');
  await page.click("[data-action=behind-toggle]");
  const early = !(await page.$("[data-call]"));
  release();
  await page.waitForSelector(".panel.behind .flow");
  await page.waitForSelector(".behind-trail .trail");
  await page.unroute(calls);
  const people = await page.$$eval("[data-call]", (els) => els.map((e) => e.dataset.call));
  check(1, "the toggle, pressed while the calls run, ends on the overview and a trail", early && people.length === 8, `${people.length} people`);
  await page.screenshot({ path: path.join(OUT, "01-simulation-behind-the-scenes.png"), fullPage: false });
  transcript.overview = await page.$eval(".panel.behind", (e) => e.innerText);

  transcript.trails = Object.fromEntries(Object.entries(await checkTrails(page, "simulation")).map(([k, t]) => [k, t.text]));
  check(4, "Ivy's work anniversary, months away, is not listed", !/work anniversary/.test(transcript.trails.X901), transcript.trails.X901.slice(0, 200));
  await page.click('[data-call="D901"]');
  await page.waitForFunction(() => document.querySelector('[data-call="D901"]')?.classList.contains("selected"));
  await (await page.$(".behind-trail")).screenshot({ path: path.join(OUT, "02-simulation-trail-backend.png") });

  // Ask about Mara: the four suggested questions, with a question half typed, then a free-text one.
  await page.click('[data-call="X900"]');
  await page.waitForFunction(() => document.querySelector('[data-call="X900"]')?.classList.contains("selected") && document.querySelector(".behind-trail .trail"));
  const before = callsRuns;
  await page.fill(".ask-form input", "What did they ask");
  for (const key of ["jd", "team", "shipped", "way_in"]) {
    const n = await page.$$eval(".answer .reason-list", (a) => a.length);
    await page.click(`[data-action=ask-suggested][data-key=${key}]`);
    await page.waitForFunction((n) => document.querySelectorAll(".answer .reason-list").length === n + 1, n);
  }
  const trailLinks = new Set(await page.$$eval(".trail-item a", (as) => as.map((a) => a.href)));
  const suggested = await page.$$eval(".answer", (as) => as.map((a) => ({ cites: [...a.querySelectorAll("a.cite")].map((c) => c.href), bare: a.querySelectorAll("span.cite").length })));
  check(12, "the suggested answers cite items the trail shows, each linked", suggested.some((a) => a.cites.length) && suggested.every((a) => a.bare === 0 && a.cites.every((h) => trailLinks.has(h))), suggested.map((a) => a.cites.length).join(","));
  const kept = await page.$eval(".ask-form input", (i) => i.value);
  check(14, "a question half typed survives the answers arriving", kept === "What did they ask", JSON.stringify(kept));
  await page.fill(".ask-form input", "What did they ask for in public?");
  await page.press(".ask-form input", "Enter");
  await page.waitForFunction(() => document.querySelectorAll(".answer .reason-list").length === 5);
  check(13, "asking never reruns the engine for the page", callsRuns === before && canned === 1, `${callsRuns - before} extra runs, ${canned} canned`);
  transcript.answers = await page.$$eval(".answer", (as) => as.map((a) => a.innerText));
  const costs = await page.$$eval(".answer p.small span.muted", (s) => s.map((x) => x.innerText));
  check(15, "the model's answer says what it cost; the suggested ones say free", /claude-opus-5, \$0\.021/.test(costs[0]) && costs.slice(1).every((c) => c === "from the stored data, free"), costs.join(" | "));
  await (await page.$(".section.ask")).screenshot({ path: path.join(OUT, "03-simulation-ask.png") });

  const text = await page.evaluate(() => document.body.innerText);
  check(2, "no source is named by the tool that pulls it", !TOOLS.test(text) && !Object.values(transcript.trails).some((t) => TOOLS.test(t)) && !TOOLS.test(transcript.overview) && !transcript.answers.some((a) => TOOLS.test(a)));

  await page.setViewportSize({ width: 390, height: 900 });
  const wide = await page.evaluate(() => document.documentElement.scrollWidth - window.innerWidth);
  check(11, "no sideways scroll at phone width", wide <= 1, `${wide}px`);
  await page.screenshot({ path: path.join(OUT, "04-phone.png"), fullPage: false });
  await page.setViewportSize({ width: 1440, height: 1000 });

  // The same people in a live workspace, with Tamsin: when each item was pulled, and the daily run's log.
  await page.fill(".ask-form input", "Half-typed question");
  await page.click("[data-mode=live]");
  await page.waitForSelector("[data-call]");
  await page.waitForSelector(".panel.behind table");
  await page.waitForSelector(".behind-trail .trail");
  const live = await page.$eval(".panel.behind", (e) => e.innerText);
  check(9, "switching workspace shows the new workspace's panel", /The daily run/.test(live) && !/no pull ran/.test(live));
  transcript.live_overview = live;
  await (await page.$(".panel.behind")).screenshot({ path: path.join(OUT, "05-live-behind-the-scenes.png") });
  const [sources, runs] = await page.$$eval(".panel.behind table", (ts) => ts.map((t) => [...t.querySelectorAll("tbody tr")].map((r) => [...r.children].map((c) => c.innerText))));
  const stored = Object.fromEntries(sources.map((r) => [r[0], r[4]]));
  check(8, "live counts what the last pull stored: Mara's one post", stored["Public posts on X"] === "1" && Object.entries(stored).every(([k, v]) => k === "Public posts on X" || v === "0"), JSON.stringify(stored));
  check(8, "the runs are newest first and say which posted", JSON.stringify(runs.map((r) => [r[2], r[5]])) === JSON.stringify([["46", "Posted"], ["3", "Nothing posted"]]), JSON.stringify(runs));

  const liveTrails = await checkTrails(page, "live");
  const states = liveTrails.E900.items.flatMap((i) => i.engine.map((l) => l.state));
  check(5, "Tamsin: the hold that is over reads closed, the one to come later, the paper's closed reason fading", ["closed", "later", "fading"].every((s) => states.includes(s)) && !states.includes("hold"), states.join(", "));
  await page.click('[data-call="X900"]');
  await page.waitForFunction(() => document.querySelector('[data-call="X900"]')?.classList.contains("selected") && /pulled /.test(document.querySelector(".behind-trail")?.innerText || ""));
  const mara = await trailOf(page);
  const left = await page.$$eval(".answer", (a) => a.length);
  check(14, "the switch goes through with a question half typed, and Mara's simulation answers are gone in live", left === 0 && (await page.evaluate(() => localStorage.getItem("gi-mode"))) === "live" && (await page.$eval(".ask-form input", (i) => i.value)) === "", `${left} answers left`);
  transcript.live_trails = Object.fromEntries(Object.entries(liveTrails).map(([k, t]) => [k, t.text]));
  check(8, "live items say when they were pulled", mara.items.every((i) => /pulled /.test(i.text)));
  check(7, "a post that touches something personal is dated and linked, never quoted", !mara.text.includes("surgery") && /not quoted: it touches something personal/.test(mara.text));
  await (await page.$(".behind-trail")).screenshot({ path: path.join(OUT, "06-live-trail-personal-post.png") });

  // The stand-ins for what the Mac found on real data (16 to 20).
  const untold = mara.items.filter((i) => /^\(not quoted: nothing in it was read/.test(i.quote));
  check(16, "Mara's posts the reader read nothing off are a count, or dated and linked without their words when behind the call", mara.hidden >= 1 && mara.hidden + untold.length >= 3 && untold.every((i) => i.call), `${mara.hidden} not shown, ${untold.length} behind the call`);
  const pinged = fs.readFileSync(path.join(TMP, "pinged.txt"), "utf8").trim();
  const asks = { X900: await askAll(page, "X900", ["jd", "team", "shipped", "way_in"]), D903: await askAll(page, "D903", ["jd", "shipped"]),
    D901: await askAll(page, "D901", ["jd", "shipped"]), [pinged]: { ...(await askAll(page, pinged, ["way_in"])) } };
  const liveAnswers = Object.values(asks).flatMap((a) => ["jd", "team", "shipped", "way_in"].filter((k) => a[k]).map((k) => a[k].text));
  transcript.live_answers = Object.fromEntries(Object.entries(asks).map(([k, a]) => [k, Object.fromEntries(Object.entries(a).filter(([q]) => q !== "trailLinks").map(([q, v]) => [q, v.lines]))]));
  const everywhere = (await page.evaluate(() => document.body.innerText)) + Object.values(liveTrails).map((t) => t.text).join(" ") + liveAnswers.join(" ");
  check(16, "no untagged post's words anywhere: page, trails or answers", UNTAGGED.every((q) => !everywhere.includes(q)) && !/fucking|culture-war|app icon/i.test(everywhere));
  for (const [who, a] of Object.entries(asks).filter(([, a]) => a.jd)) {
    const phrases = a.jd.lines.flatMap((l) => (/^Uses the job description's phrases? (.*) \(topic:/.exec(l)?.[1].match(/"[^"]+"/g) || []).map((q) => q.slice(1, -1)));
    const single = phrases.filter((q) => !/[\s-]/.test(q) && q === q.toLowerCase());
    check(17, `${who}: the job description match rests on phrases, not single common words`, single.length === 0 && !/"(bring|back|app)"/.test(a.jd.text), single.join(", "));
    check(17, `${who}: the job description match cites only items the trail shows`, a.jd.cites.every((h) => a.trailLinks.includes(h)), a.jd.cites.join(" "));
    check(17, `${who}: the job description match lists its phrases, or says plainly that none matches`, (phrases.length > 0) !== a.jd.lines.some((l) => /^No phrase from the/.test(l)), a.jd.lines.join(" | "));
    const [total, , ...kinds] = (/tagged (\d+) of their (\d+).*?: (\d+) on its topics, (\d+) work in progress, (\d+) a public ask GI could answer, (\d+) a release or launch, (\d+) a paper out/.exec(a.jd.lines[0]) || []).slice(1).map(Number);
    const sum = kinds.reduce((x, y) => x + y, 0);
    check(30, `${who}: the count's kinds add up to its total, or it says some items count under more than one`, kinds.length === 5 && sum >= total && (sum > total) === /; some items count under more than one kind;/.test(a.jd.lines[0]), a.jd.lines[0]);
  }
  const SHIPPED = /^(\d{1,2} [A-Z][a-z]{2} \d{4}, [^:]+: (a release|a paper|a launch)\b|No release, paper or launch of theirs in the last 90 days\.$)/;
  for (const [who, a] of Object.entries(asks).filter(([, a]) => a.shipped)) {
    check(18, `${who}: what they shipped lists only releases, papers and launches`, a.shipped.lines.every((l) => SHIPPED.test(l)), a.shipped.lines.join(" | "));
  }
  const counted = asks.D901.jd.lines[0];
  check(23, "Theo's job description match cites his release, and its count names releases, launches and papers", asks.D901.jd.cites.some((h) => h.includes("tanglewood")) && /[1-9]\d* a release or launch, \d+ a paper out(; some items count under more than one kind)?;/.test(counted) && /(\d+ papers? of theirs (is|are)|No paper of theirs is) on file from the paper indexes/.test(counted), asks.D901.jd.lines.join(" | "));
  check(22, "no answer quotes the post someone replied to", liveAnswers.every((a) => !a.includes(ANSWERED) && !/In reply to/.test(a)));
  check(18, "Theo's release is what he shipped", asks.D901.shipped.lines.some((l) => /a release/.test(l)) && asks.D901.shipped.cites.some((h) => h.includes("tanglewood")), asks.D901.shipped.lines.join(" | "));
  check(19, `the way in for ${pinged}, already on a card, names the role by its title`, /on a card/i.test(asks[pinged].way_in.text) && !DEV_WORDS.test(asks[pinged].way_in.text), asks[pinged].way_in.lines.join(" | "));
  check(19, "no developer words on the page or in any answer", !DEV_WORDS.test(everywhere + transcript.overview + live), (DEV_WORDS.exec(everywhere + transcript.overview + live) || [""])[0]);
  const theo = liveTrails.D901.items.find((i) => i.link?.includes("v1.0"));
  check(20, "Theo's release reads as a release that happened, counted where the call counts it", theo && theo.read === 1 && /a release/.test(theo.text) && theo.engine.every((l) => !/next few weeks/.test(l.text)), theo ? theo.engine.map((l) => `${l.state}: ${l.text}`).join(" | ") : "not shown");
  const lena = liveTrails.D904.items.find((i) => i.link?.includes("status/9001"));
  check(21, "Lena's launch, out three days ago, holds her call as her launch week, never a launch still to come", lena && lena.engine.some((l) => l.state === "hold" && /their launch week/.test(l.text)) && !/next few weeks/.test(liveTrails.D904.text + liveTrails.D904.detail), lena ? lena.engine.map((l) => `${l.state}: ${l.text}`).join(" | ") : "not shown");
  const said = (i) => (i ? i.engine.map((l) => `${l.state}: ${l.text}`).join(" | ") || "nothing" : "not shown");
  const [v03, next] = ["v0.3", "status/1"].map((v) => liveTrails.E901.items.find((i) => i.link?.endsWith(v)));
  check(26, "Rowan's release is not counted, and never said to be set aside for the newer one, which only holds or waits", v03 && ["holds", "waits"].includes(next?.call) && v03.engine.length === 1 && v03.engine[0].text === "Not counted toward the call", `${said(v03)} / newer: ${next?.call}`);
  const [v1, v095] = ["v1.0", "v0.9.5"].map((v) => liveTrails.D901.items.find((i) => i.link?.endsWith(v)));
  const publicDay = (i) => (/Public (\d+ \w+ \d{4}|\w+ \d+, \d{4}|\w+ \d+)/.exec(i?.text || "") || [])[1];
  check(26, "Theo's v0.9.5 is set aside for the newer release the call counts, on its day", v095 && ["trigger", "listed", "counted"].includes(v1?.call) && v095.engine.length === 1 && v095.engine[0].text === `Not counted: the call counts the newer release from ${publicDay(v1)}`, `${said(v095)} / v1.0: ${v1?.call}, public ${publicDay(v1)}`);
  const reply = mara.items.find((i) => i.link?.endsWith("status/1012"));
  check(28, "Mara's reply shows her own words, whole, and nothing of the post she answered", reply?.quote === "Our world models now train on twice the gameplay hours.", reply ? reply.quote : "not shown");
  const v08 = liveTrails.D901.items.find((i) => i.link?.endsWith("v0.8"));
  check(29, "Theo's release no one read shows its line, as a release", v08 && /Tanglewood 0\.8/.test(v08.quote) && v08.read === 1 && /a release/.test(v08.text), v08 ? v08.text.replace(/\s+/g, " ").slice(0, 200) : `not shown; hidden ${liveTrails.D901.hidden}`);
  await page.click('[data-call="D901"]');
  await page.waitForFunction(() => document.querySelector('[data-call="D901"]')?.classList.contains("selected") && /tanglewood/.test(document.querySelector(".behind-trail")?.innerHTML || ""));
  await (await page.$(".behind-trail")).screenshot({ path: path.join(OUT, "07-live-trail-release.png") });

  const reads = trailReads;
  await page.click("[data-action=calls-refresh]");
  await page.waitForFunction((n) => document.querySelector(".behind-trail .trail") && window.performance.getEntriesByType("resource").filter((e) => e.name.includes("/trail?")).length > n, reads);
  check(9, "Refresh reads the shown person's trail again", trailReads > reads, `${trailReads - reads} new reads`);

  const shown = await page.evaluate(() => document.body.innerText);
  check(10, "no undefined or NaN on screen", !/undefined|NaN/.test(shown + Object.values(transcript.trails).join(" ") + transcript.overview + live + transcript.answers.join(" ")));
  const status = async (u) => (await page.request.get(BASE + u)).status();
  const asked = async (who, body) => (await page.request.post(`${BASE}/api/calls/${who}/ask`, { data: { mode: "simulation", ...body } })).status();
  check(10, "an unknown person gets a 404, an unknown workspace or question a 422", (await status("/api/calls/nobody/trail?mode=simulation")) === 404 && (await status("/api/calls/sources?mode=nowhere")) === 422 && (await status("/api/calls/X900/trail?mode=nowhere")) === 422 && (await asked("nobody", { suggested: "jd" })) === 404 && (await asked("X900", { suggested: "pay" })) === 422);
  check(10, "no page errors", errors.length === 0, errors.join(" | "));
}

(async () => {
  fs.mkdirSync(OUT, { recursive: true });
  const env = seed();
  const server = await up(env);
  const browser = await playwright().chromium.launch();
  let failed = null;
  try {
    await run(await browser.newPage({ viewport: { width: 1440, height: 1000 } }));
  } catch (e) {
    failed = e;
  } finally {
    await browser.close();
    server.kill();
    fs.rmSync(TMP, { recursive: true, force: true });
    fs.writeFileSync(path.join(OUT, "transcript.json"), JSON.stringify(transcript, null, 1));
    fs.writeFileSync(path.join(OUT, "checks.txt"), checks.join("\n") + "\n" + (failed ? `STOPPED: ${failed.message}\n` : "All checks passed.\n"));
  }
  console.log(`${checks.join("\n")}\n${failed ? "FAILED: " + failed.message : "All checks passed."}\nArtifact: ${OUT}`);
  process.exit(failed ? 1 : 0);
})();
