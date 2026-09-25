// End to end: the fixed "New job posts" preview (what watching GI and Medal's job board would look like), driven in a
// real browser against the real app. The page is hardcoded and says so: nothing on it is live, it makes no request,
// and its people are invented. The app runs on temp stores; the one Add is answered in the browser, so nothing is
// written and nothing spends.
//
//   node tests/e2e/board_watch.cjs [out-dir]      (default: a new folder under the system temp dir)
//
// Needs uv and Playwright with Chromium, as tests/e2e/todays_calls.cjs does. The artifact is the out-dir: a
// screenshot of the page at desktop and phone width, and checks.txt (each check below, passed).
//
// Ways it could fail, written before the page (Justin's rule, 2026-09-24):
//  1. The drop box or the box after Add has no way to the page, or the way there leaves a dialog open.
//  2. The page doesn't open with a "Preview: not built yet" banner, above everything else on it.
//  3. The page fetches anything: an API call, a model call, another host. (The app's own state poll, every 5 seconds on
//     every page, is not the page's and is left out.)
//  4. The new post it shows is not a real post on GI and Medal's board, or is already a role here; or the page calls
//     the board GI's alone.
//  5. A person on it is not labelled invented, a source is named by the tool that pulls it, or a source and time read
//     as a placeholder ("X · yesterday").
//  6. "undefined" or "NaN" on screen, a page error, or sideways scroll at phone width.
//  7. The live workspace strip ("Live workspace.") or the Live/Simulation switch shows on the preview, calling a
//     made-up page live.
//  8. In-house words a hiring manager wouldn't use: "free check", "worked test cases", "a replay check", or "Counts"
//     and "Holds" as labels. (Added 2026-09-24 18:25, before the fix.)
//  9. On a laptop screen (1280x800), the drop box's or the preview box's buttons sit below the box's edge.
//     (Added 2026-09-24 19:10, before the fix, as are check 7's switch and check 5's placeholder.)
// 10. A read that fails says so where it can be seen: the box's error line is below its buttons, off screen while the
//     buttons stay in view. (Added 2026-09-24 19:35, from the review, before the fix.)
const { execFileSync, spawn } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");

const ROOT = path.resolve(__dirname, "../..");
const OUT = path.resolve(process.argv[2] || fs.mkdtempSync(path.join(os.tmpdir(), "gi-e2e-board-")));
fs.mkdirSync(OUT, { recursive: true });
const TMP = fs.mkdtempSync(path.join(os.tmpdir(), "gi-e2e-board-work-"));
const PORT = 8890 + Math.floor(Math.random() * 100);
const BASE = `http://127.0.0.1:${PORT}`;
const JARGON = /free check|worked test|replay (check|drill)|pre-?registered|\bgolden\b|\bdetectors?\b/i; // a "replay viewer" someone built is their work
const TOOLS = /\b(apify|apidojo|harvestapi|crustdata|exa|phantombuster)\b/i;
// Posts on GI and Medal's board (jobs.ashbyhq.com/generalintuition-medal) that a web search listed on 2026-09-24 and
// that are not roles here: the page says the post it shows is new and not a role yet.
const ROLES = JSON.parse(fs.readFileSync(path.join(ROOT, "config/roles.json"), "utf8")).roles.map((r) => r.title);
const BOARD_ONLY = ["Senior Frontend Engineer (React)", "Technical Interns and New Grads"].filter((t) => !ROLES.includes(t));

function playwright() {
  try {
    return require("playwright");
  } catch {
    return require(path.join(execFileSync("npm", ["root", "-g"]).toString().trim(), "playwright"));
  }
}

async function up() {
  const env = { ...process.env, ANTHROPIC_API_KEY: "e2e-made-up-key", TIMELINES_DB: path.join(TMP, "timelines.sqlite"), SOCIAL_DIR: TMP, DATABASE_URL: `sqlite:///${path.join(TMP, "pilot.sqlite")}` };
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

const checks = [];
function check(n, what, ok, detail = "") {
  checks.push(`${ok ? "PASS" : "FAIL"} ${n}. ${what}${detail ? ` (${detail})` : ""}`);
  if (!ok) throw new Error(`Check ${n} failed: ${what} ${detail}`);
}

// Opens the page with the given click, then reads it as it shows, with every request from that click on.
async function onPage(page, open) {
  const requests = [];
  const seen = (r) => r.url().startsWith(`${BASE}/api/state?`) || requests.push(r.url());
  page.on("request", seen);
  await page.click(open);
  await page.waitForSelector(".board-watch");
  check(1, "the way there closes the dialog", (await page.locator("dialog[open]").count()) === 0);
  await page.waitForTimeout(6000); // anything the page would fetch on its own, past one state poll
  const shown = await page.$eval("#main", (m) => ({
    first: m.querySelector(".board-watch")?.firstElementChild?.innerText || "",
    text: m.innerText,
    post: m.querySelector("[data-post]")?.innerText || "",
    people: [...m.querySelectorAll("[data-person]")].map((p) => p.innerText),
    labels: [...m.querySelectorAll(".board-label")].map((l) => l.textContent.trim()),
    strip: Boolean(document.querySelector(".mode-banner.live")),
    toggle: Boolean(document.querySelector("[data-mode]")),
  }));
  page.off("request", seen);
  check(2, 'the page opens with "Preview: not built yet"', shown.first.startsWith("Preview: not built yet"), shown.first.slice(0, 60));
  check(3, "the page fetches nothing", requests.length === 0, requests.join(", "));
  check(4, "the new post is a real post on the board that is not a role here", BOARD_ONLY.includes(shown.post), shown.post);
  const alone = shown.text.match(/\bGI's (own )?(job )?board\b|\bGI('s)? (own )?(job )?posts?\b/);
  check(4, "the board is GI and Medal's, never GI's alone", shown.text.includes("GI and Medal's job board") && !alone, alone?.[0]);
  check(5, "every person is labelled invented", shown.people.length >= 2 && shown.people.every((p) => /invented/i.test(p)), `${shown.people.length} people`);
  check(5, "no source is named by the tool that pulls it", !TOOLS.test(shown.text));
  check(5, "each item says where and when in words", shown.people.every((p) => /\bOn (X|GitHub|LinkedIn), /.test(p)), shown.people.find((p) => !/\bOn (X|GitHub|LinkedIn), /.test(p)));
  check(6, "no undefined or NaN on screen", !/\bundefined\b|\bNaN\b/.test(shown.text));
  check(7, "no live workspace strip above the preview", !shown.strip);
  check(7, "no Live/Simulation switch on the preview", !shown.toggle);
  check(8, "no in-house words, and labels a hiring manager would use", !JARGON.test(shown.text) && shown.labels.join("|") === "Reasons to reach out|Reasons to wait", shown.text.match(JARGON)?.[0] || shown.labels.join(", "));
  return shown;
}

(async () => {
  const server = await up();
  const { chromium } = playwright();
  const browser = await chromium.launch(fs.existsSync("/opt/pw-browsers/chromium-1194/chrome-linux/chrome") ? { executablePath: "/opt/pw-browsers/chromium-1194/chrome-linux/chrome" } : {});
  const errors = [];
  try {
    const page = await browser.newPage({ viewport: { width: 1280, height: 1000 } });
    page.on("pageerror", (e) => errors.push(e.message));
    await page.goto(BASE);
    await page.click("text=Role briefs");
    await page.click('button:has-text("Drop in a JD")');
    check(4, "the drop box names GI and Medal's job board", /GI and Medal's job board/.test(await page.textContent("dialog[open] .hint")));
    await onPage(page, '[data-action="board-open"]');
    await page.screenshot({ path: path.join(OUT, "board-watch.png"), fullPage: true });

    // Live: a sample preview passed off as a real read in the browser, and its Add answered there too.
    const sample = await (await fetch(`${BASE}/api/roles/preview`, { method: "POST", headers: { "content-type": "application/json" }, body: JSON.stringify({ sample: true }) })).json();
    await page.route("**/api/roles/preview", (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...sample, sample: false }) }));
    await page.route("**/api/roles/accept", (r) => r.fulfill({ status: 200, contentType: "application/json", body: JSON.stringify({ ...sample.role, next: ["Nobody is watched for this role yet, so it makes no calls."] }) }));
    await page.click('[data-view="roles"]'); // off the preview: the switch isn't on it
    await page.click('[data-mode="live"]');
    await page.click('[data-view="roles"]');
    await page.click('button:has-text("Drop in a JD")');
    await page.fill("#drop-jd", sample.jd_text);
    await page.click('button:has-text("Read this JD")');
    await page.click('dialog[open] button:has-text("Add ")');
    await page.waitForSelector('dialog[open] [data-action="board-open"]'); // the box after Add: its request is done
    await onPage(page, 'dialog[open] [data-action="board-open"]');

    // A laptop screen: each box's buttons are on screen and not covered, with nothing scrolled.
    const laptop = await browser.newPage({ viewport: { width: 1280, height: 800 } });
    laptop.on("pageerror", (e) => errors.push(e.message));
    let reads = 0; // the first read fails, as a model error would
    await laptop.route("**/api/roles/preview", (r) => r.fulfill(reads++ === 0
      ? { status: 502, contentType: "application/json", body: JSON.stringify({ detail: "The model didn't answer. Nothing was read or charged twice." }) }
      : { status: 200, contentType: "application/json", body: JSON.stringify({ ...sample, sample: false }) }));
    await laptop.goto(BASE);
    await laptop.click('[data-mode="live"]');
    await laptop.click('[data-view="roles"]');
    const reachable = (label) => laptop.$eval(`dialog[open] button:text-is("${label}")`, (b) => {
      const r = b.getBoundingClientRect(), d = b.closest("dialog").getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
      return r.bottom <= Math.min(innerHeight, d.bottom) && r.top >= d.top && (hit === b || b.contains(hit));
    });
    await laptop.click('button:has-text("Drop in a JD")');
    check(9, "the drop box's Read this JD button is on a laptop screen", await reachable("Read this JD"));
    await laptop.fill("#drop-jd", sample.jd_text);
    await laptop.click('button:has-text("Read this JD")');
    await laptop.waitForFunction(() => document.querySelector("#dialog-error")?.textContent.includes("didn't answer"));
    const shownError = await laptop.$eval("#dialog-error", (e) => {
      const r = e.getBoundingClientRect(), d = e.closest("dialog").getBoundingClientRect();
      const hit = document.elementFromPoint(r.left + 4, r.top + r.height / 2);
      return r.top >= d.top && r.bottom <= Math.min(innerHeight, d.bottom) && (hit === e || e.contains(hit));
    });
    check(10, "a failed read says so where it can be seen", shownError);
    await laptop.click('button:has-text("Read this JD")');
    await laptop.waitForSelector('dialog[open] #role-accept-form');
    check(9, "the preview box's Add button is on a laptop screen", await reachable(`Add ${sample.role.title}`));

    const phone = await browser.newPage({ viewport: { width: 390, height: 844 } });
    phone.on("pageerror", (e) => errors.push(e.message));
    await phone.goto(BASE);
    await phone.click('[data-view="roles"]');
    await phone.click('button:has-text("Drop in a JD")');
    await phone.click('[data-action="board-open"]');
    await phone.waitForSelector(".board-watch");
    const wide = await phone.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    check(6, "no sideways scroll at phone width", wide <= 1, `${wide}px`);
    await phone.screenshot({ path: path.join(OUT, "board-watch-phone.png"), fullPage: true });
    check(6, "no page error", errors.length === 0, errors.join("; "));
  } finally {
    fs.writeFileSync(path.join(OUT, "checks.txt"), checks.join("\n") + "\n");
    await browser.close();
    server.kill();
  }
  console.log(checks.join("\n") + `\nAll ${checks.length} checks passed. Out: ${OUT}`);
})().catch((e) => {
  console.error(e.message);
  process.exit(1);
});
