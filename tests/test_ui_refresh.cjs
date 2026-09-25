const assert = require('node:assert/strict');
const { test } = require('node:test');
const vm = require('node:vm');
const { readFileSync } = require('node:fs');
const { join } = require('node:path');

// Exercise the shipped refresh function with controlled network timing. No real
// backend or candidate records are modified by these tests.
const source = readFileSync(join(__dirname, '../app/static/app.js'), 'utf8');
const refreshSource = source.slice(source.indexOf('async function refresh('), source.indexOf('\nfunction nav()'));
function harness(view = 'sequence') {
  let renders = 0;
  let openDetails = false;
  const context = vm.createContext({
    ui: { mode: 'live', view, state: {}, dirty: false },
    document: { activeElement: { tagName: 'SUMMARY' } },
    $: (selector) => selector === '#dialog' ? { open: false } : selector === '#main details[open]' && openDetails ? {} : null,
    api: async () => ({ refreshed: true }),
    render: () => renders++,
    loadSourcing: async () => {},  // Today's calls' behind-the-scenes view: its own end-to-end run covers it
  });
  vm.runInContext(refreshSource, context);
  return { context, refresh: (force = false) => context.refresh(force), renders: () => renders, openDetails: () => { openDetails = true; } };
}

test('repeated polling updates data without replacing a form being filled in', async () => {
  const h = harness('watchlist');
  h.context.document.activeElement.tagName = 'INPUT';
  await h.refresh();
  await h.refresh();
  assert.equal(h.context.ui.state.refreshed, true);
  assert.equal(h.renders(), 0);
});

test('a disclosure opened while a poll was pending is not closed by it', async () => {
  const h = harness('watchlist');
  h.context.document.activeElement.tagName = 'BODY';
  let resolve;
  h.context.api = () => new Promise((done) => { resolve = done; });
  const request = h.refresh();
  h.openDetails();
  resolve({ refreshed: true });
  await request;
  assert.equal(h.renders(), 0);
});

test('other pages also keep expanded disclosures open during polling', async () => {
  const h = harness('sequence');
  h.openDetails();
  await h.refresh();
  assert.equal(h.renders(), 0);
});

test('ordinary live pages still render fresh data when not being edited', async () => {
  const h = harness('watchlist');
  h.context.document.activeElement.tagName = 'BODY';
  await h.refresh();
  assert.equal(h.renders(), 1);
});

test('the poll never repaints an engine page it did not reload, so unsaved choices stay', async () => {
  const h = harness('hiring');
  h.context.document.activeElement.tagName = 'BODY';
  h.context.ui.hiring = { loaded: true };
  await h.refresh();
  assert.equal(h.renders(), 0);
  h.context.ui.hiring = null;  // not loaded yet: the poll loads it and shows it
  await h.refresh();
  assert.equal(h.renders(), 1);
  assert.equal(h.context.ui.error, '');
});

test('explicit actions can still request a refresh while a disclosure is open', async () => {
  const h = harness('sequence');
  h.openDetails();
  await h.refresh(true);
  assert.equal(h.renders(), 1);
});

function purposeHarness() {
  const context = vm.createContext({
    localStorage: { getItem: () => null },
    URL,
    sourceExcerpt: () => '',
  });
  vm.runInContext(source.slice(0, source.indexOf('async function api(')), context);
  vm.runInContext(source.slice(source.indexOf('function candidateDetail('), source.indexOf('\nfunction crustdataPanel(')), context);
  vm.runInContext(source.slice(source.indexOf('function sequenceAlert('), source.indexOf('\nfunction sequenceDemoResult(')), context);
  vm.runInContext('ui.state = { roles: [], candidates: [] }', context);
  return context;
}

test('contact purpose distinguishes rapport from hiring and legacy records require reassessment', () => {
  const h = purposeHarness();
  const ground = { statement: 'Their request is for technical feedback this week.', evidence_id: 'source-1', quote: 'Please share feedback this week.' };
  const event = { title: 'Feedback requested', timing_action: 'contact_now', timing_assessment: { mechanism: 'active_work_need', person_impact: ground, opportunity: ground, waiting_cost: ground } };
  const legacy = h.candidateDetail({ events: [event] });
  assert.match(legacy, /Purpose not assessed/);
  assert.match(legacy, /No justified outreach time/);
  assert.doesNotMatch(legacy, /Now, provisionally/);
  const rapport = h.candidateDetail({ events: [{ ...event, contact_purpose: 'rapport', purpose_reason: ground }] });
  assert.match(rapport, /Build rapport/);
  assert.match(rapport, /Rapport is not evidence of job-seeking/);
  assert.match(rapport, /Their request is for technical feedback this week/);
  assert.match(rapport, /href="#evidence-source-1"/);
  assert.match(rapport, /Now, provisionally/);
  const hiring = h.sequenceAlert({ ping: { contact_purpose: 'hiring', purpose_reason: ground, person: { name: 'Test person' } } });
  assert.match(hiring, /Discuss hiring/);
  assert.match(hiring, /does not establish willingness to change jobs/);
});

test('purpose evidence is escaped in a ping', () => {
  const h = purposeHarness();
  const ground = { statement: '<script>unsafe</script>', evidence_id: 'source-1', quote: '<img src=x onerror=unsafe>' };
  const ping = h.sequenceAlert({ ping: { contact_purpose: 'rapport', purpose_reason: ground, person: { name: 'Test person' } } });
  assert.match(ping, /&lt;script&gt;unsafe&lt;\/script&gt;/);
  assert.doesNotMatch(ping, /<script>|<img /);
});

test('statement dates remain visibly separate from career milestone dates', () => {
  const h = purposeHarness();
  const html = h.candidateDetail({ events: [{ date_basis: 'source_statement_date', date: '2031-06-14', date_caveat: 'The date describes the public request, not the project completion.' }] });
  assert.match(html, /Source statement date/);
  assert.match(html, /Statement date, not a verified career or project milestone/);
  assert.match(html, /The date describes the public request, not the project completion/);
});

test('separate evidence passages stay separate and escaped', () => {
  const h = purposeHarness();
  vm.runInContext(source.slice(source.indexOf('function sourceExcerpt('), source.indexOf('\nfunction runDetails(')), h);
  const html = h.sourceExcerpt({excerpt:'First quoted passage.',additional_excerpts:['<script>second passage</script>']});
  assert.equal((html.match(/<blockquote>/g)||[]).length, 2);
  assert.match(html, /Separate passage from the same source/);
  assert.match(html, /&lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
});

test('the timing label follows readiness, not the model event, whenever the person has a timeline', () => {
  const h = purposeHarness();
  const ground = { statement: 'Their request is for technical feedback this week.', evidence_id: 'source-1', quote: 'Please share feedback this week.' };
  const hook = { id: 'hook', title: 'A dated update', timing_action: 'watch', contact_purpose: 'rapport', purpose_reason: ground };
  const reach = { action: 'reach_now', confidence: 'high', window: { detector_id: 'own_departure', opens: '2026-09-21T00:00:00+00:00', closes: '2026-11-05T00:00:00+00:00' } };
  const ready = h.candidateDetail({ events: [hook], decision: { state: 'ready', event_id: 'hook', readiness: reach } });
  assert.match(ready, /Suggested outreach timing: Now: the timeline says reach out/);
  assert.match(ready, /Sep 21, 2026 to Nov 5, 2026/);
  assert.doesNotMatch(ready, /keep watching/);
  const wait = h.candidateDetail({ events: [], decision: { state: 'watch', readiness: { action: 'respect_follow_up', until: '2026-10-20T00:00:00+00:00', confidence: 'low' } } });
  assert.match(wait, /Suggested outreach timing: Wait until Oct 20, 2026/);
  assert.match(wait, /Readiness: respect follow up \(low confidence\)/);
  const modelOnly = h.candidateDetail({ events: [hook], decision: { state: 'watch', event_id: 'hook' } });
  assert.match(modelOnly, /No outreach time established — keep watching/);
  // Under reach now, a hold on the hook itself still shows.
  const held = h.candidateDetail({ events: [{ ...hook, timing_action: 'follow_up', follow_up_on: '2026-10-20' }], decision: { state: 'watch', event_id: 'hook', readiness: reach } });
  assert.match(held, /Suggested outreach timing: Use the source-backed follow-up date below/);
  const killed = h.candidateDetail({ events: [{ ...hook, timing_action: 'verify_first' }], decision: { state: 'research', readiness: reach } });
  assert.match(killed, /Suggested outreach timing: Verify first: the trigger was contradicted or superseded/);
});

function callsHarness() {
  const context = vm.createContext({ localStorage: { getItem: () => null }, URL, window: { innerWidth: 1400 } });
  vm.runInContext(source.slice(0, source.indexOf('async function api(')), context);
  vm.runInContext(source.slice(source.indexOf('function heading('), source.indexOf('\nfunction reviewPage(')), context);
  return context;
}
const reach = {
  subject_id: 'X1', name: 'Ada <b>Lin</b>', employer: 'Lab', profile_url: 'https://x.com/ada', role: { title: 'Product Designer', jd_url: 'https://jobs.example/pd' },
  action: 'reach_now', headline: 'Reach out now', track: 'Pitch the role', until: null, why_now: 'Open on 2026-09-15: work in progress they posted.',
  trigger: { id: 'e1', date: '2026-09-05', what: 'work in progress they posted', quote: 'Building <script>x</script>', source_url: 'https://x.com/ada/1' },
  signals: [], confidence: 'high', falsifiers: ['They start a new role elsewhere'], next_step: 'Reach out before 2026-09-26.',
  draft: { to: 'ada@example.org', subject: 'Hello', body: 'Hi Ada' },
  route: { paths: [], channel: 'email', target: 'ada@example.org', reason: 'Listed publicly.', url: 'https://mail.google.com/x', open: 'Open the email' },
};

test('a call opens into the seven parts of a ping, escaped', () => {
  const h = callsHarness();
  const html = h.pingDetail(reach, { team_loaded: false });
  for (const part of ['Person', 'Role', 'Trigger', 'Why now', 'Confidence, and what would prove it wrong', 'Draft outreach', 'Route in']) assert.match(html, new RegExp(`</span>${part}</h3>`));
  assert.match(html, /Ada &lt;b&gt;Lin&lt;\/b&gt;/);
  assert.match(html, /Building &lt;script&gt;/);
  assert.doesNotMatch(html, /<script>/);
  assert.match(html, /Open the email ↗/);
  assert.match(html, /Warm path: none found. No GI team list is loaded/);
});

test('a wait or a check has placeholders, not an empty draft or an invented route', () => {
  const h = callsHarness();
  const wait = h.pingDetail({ ...reach, action: 'respect_follow_up', headline: 'Wait', until: '2026-11-01', track: null, draft: null, route: null }, { team_loaded: true });
  assert.match(wait, /No draft: the call is not to reach out yet/);
  assert.match(wait, /Not looked for until the call is to reach out/);
  assert.match(wait, /Wait until Nov 1, 2026/);
  const check = h.pingDetail({ ...reach, action: 'verify_first', headline: 'Check first', draft: null, route: null }, { team_loaded: true });
  assert.match(check, /No draft: check the fact first/);
});

test('a call shows what a reach is about and what just happened, with its day and source', () => {
  const h = callsHarness();
  const early = h.pingDetail({ ...reach, kind: 'early_sign', happened: [] }, { team_loaded: true });
  assert.match(early, /<span class="badge early">Early sign<\/span>/);
  assert.match(early, /<strong>Early sign\.<\/strong> The reach rests on an early sign of their own work\./);
  assert.doesNotMatch(early, /Just happened/);
  const moment = { kind: 'paper', label: 'paper out', date: '2026-09-12', day: 3, note: '', line: 'paper out · 2026-09-12 · day 3 of about <30>', source_url: 'https://news.example/1', quote: 'x' };
  const news = h.pingDetail({ ...reach, kind: 'just_happened', happened: [moment] }, { team_loaded: true });
  assert.match(news, /<span class="badge happened">Just happened · paper out<\/span>/);
  assert.match(news, /<li>paper out · 2026-09-12 · day 3 of about &lt;30&gt; · <a href="https:\/\/news\.example\/1"/);
  const check = h.pingDetail({ ...reach, action: 'verify_first', headline: 'Check first', kind: null, happened: [moment], draft: null, route: null }, { team_loaded: true });
  assert.match(check, /Just happened · paper out/);  // listed whatever the call
  assert.doesNotMatch(check, /Early sign/);
  vm.runInContext('ui.calls = CALLS', Object.assign(h, { CALLS: { as_of: '2026-09-15', source: 'Invented', calls: [
    { ...reach, kind: 'early_sign', happened: [] }, { ...reach, subject_id: 'X2', kind: 'just_happened', happened: [moment] }] } }));
  const listed = h.callsPage().split('<article')[0];
  assert.match(listed, /Reach out now<\/span><span class="badge early">Early sign<\/span>/);
  assert.match(listed, /Reach out now<\/span><span class="badge happened">Just happened · paper out<\/span>/);
});

test('the scorecard never claims a win on too few moments or when random timing does as well', () => {
  const h = callsHarness();
  assert.match(h.scorecardVerdict({ moments: 2, seen_rate: 1 }, { seen_rate: 0 }), /Too few public moments/);
  assert.match(h.scorecardVerdict({ moments: 10, seen_rate: 0.2, false_alarm_rate: 0.1 }, { seen_rate: 0.3, false_alarm_rate: 0.1 }), /did not beat random timing/);
  assert.match(h.scorecardVerdict({ moments: 10, seen_rate: 0.5, false_alarm_rate: 0.3, p: 0.01 }, { seen_rate: 0.2, false_alarm_rate: 0.1 }), /more false alarms/);
});

test('the scorecard claims a gap over random timing only when luck would not explain it', () => {
  const h = callsHarness();
  const engine = { moments: 10, seen_coming: 4, seen_rate: 0.4, ordinary_stretches: 19, fired_with_nothing_after: 5, false_alarm_rate: 5 / 19 };
  const random = { seen_rate: 0.32, false_alarm_rate: 0.32 };
  assert.equal(h.scorecardVerdict({ ...engine, p: 0.3638 }, random), 'Not distinguishable from random timing yet: 4 of 10 moments against 5 of 19 ordinary stretches, p 0.36.');
  assert.match(h.scorecardVerdict(engine, random), /Not distinguishable from random timing yet: 4 of 10 moments against 5 of 19 ordinary stretches\.$/);  // an older report, with no p
  assert.match(h.scorecardVerdict({ ...engine, p: 0.004 }, random), /^The engine saw more moments coming than random timing did, without more false alarms: .*p 0\.004\.$/);
  const sign = (kept) => ({ label: 'Work in progress', early: true, kept, before_moment: 1, before_moment_windows: 10, ordinary: 1, ordinary_windows: 19, before_moment_rate: 0.1, ordinary_rate: 0.05, lift: 2, p: 0.5 });
  const card = { engine: { ...engine, p: 0.3638, lead_weeks: { median: 4.1 } }, any_reach: { ...engine, lead_weeks: {} }, random_timing: random, obvious_news: { seen_rate: 0, lead_weeks: 0, false_alarm_rate: 0 } };
  const report = (kept) => ({ file: 'x-scorecard.json', halves: { noise_test: ['a'], scorecard: ['b'] }, config: { keep: [2, 0.05] }, noise: { signs: { work_in_progress: sign(kept) } }, dropped: kept ? [] : ['work_in_progress'], scorecard: { all_signs: card, kept_signs: { engine: { ...engine, seen_coming: 0 } } } });
  vm.runInContext('ui.mode = "live"; ui.scorecard = { report: R }', Object.assign(h, { R: report(false) }));
  const page = h.scorecardPage();
  assert.match(page, /<h2>Not distinguishable from random timing yet/);
  assert.match(page, /<td>The engine, only signs that passed the noise test<\/td><td colspan="4">n\/a: no sign was kept<\/td>/);
  assert.match(page, /Not kept, not yet shown to beat an ordinary stretch: Work in progress\./);
  assert.match(page, /1 to 7 weeks before the moment/);
  assert.doesNotMatch(page, /Dropped as noise|1 to 8 weeks|read the direction/);
  vm.runInContext('ui.scorecard = { report: R }', Object.assign(h, { R: report(true) }));
  assert.match(h.scorecardPage(), /<td>The engine, only signs that passed the noise test<\/td><td>0 of 10/);
});

test('live pages say when the data was pulled and warn when it is old; the simulation is labelled invented', () => {
  const h = callsHarness();
  vm.runInContext('ui.mode = "live"', h);
  assert.equal(h.freshLine({ line: 'Data pulled <today>.', warning: null }), '<p class="small muted fresh-line">Data pulled &lt;today&gt;.</p>');
  assert.match(h.freshLine({ line: 'Data pulled 20 September.', warning: 'The data is 4 days old.' }), /class="callout warning fresh-line"><strong>Check the pull first\.<\/strong> The data is 4 days old\./);
  assert.doesNotMatch(h.heading('Today', 'x'), /Invented data/);
  vm.runInContext('ui.mode = "simulation"', h);
  assert.equal(h.freshLine({ line: 'Invented.', warning: null }), '');
  assert.match(h.heading('Today', 'x'), /<h1>Today <span class="badge invented">Invented data<\/span><\/h1>/);
});

test('the older candidate pages fold under one entry and open when one of them is the page', () => {
  const context = vm.createContext({ ui: { view: 'calls', state: null }, getCandidates: () => [] });
  vm.runInContext(source.slice(source.indexOf('const NAV = '), source.indexOf('function getCandidates(')), context);
  const closed = context.nav();
  for (const page of ["Today's calls", 'Monday brief', 'Scorecard', 'Events', 'Role briefs', 'Setup']) assert.match(closed, new RegExp(`>${page}</button>`));
  for (const page of ['Watches &amp; alerts', 'Watches & alerts', 'Opportunity inbox', 'Watchlist', 'Sources & runs']) assert.doesNotMatch(closed, new RegExp(`>${page}<`));
  assert.match(closed, /data-action="toggle-older" aria-expanded="false" >Older pages/);
  context.ui.view = 'inbox';
  const open = context.nav();
  assert.match(open, /aria-expanded="true" disabled>Older pages/);  // open while one of them is the page
  assert.match(open, /Not the timing engine\./);
  assert.match(open, /data-view="inbox" aria-current="page">Opportunity inbox<span class="nav-count">0<\/span>/);
  assert.match(open, /data-view="sources" >Sources & runs</);
});

test('a reach the contact ledger holds shows who owns it and offers no draft to copy', () => {
  const h = callsHarness();
  const html = h.pingDetail({ ...reach, gate: { state: 'hold', reason: 'Invited to <b>Games night</b> by Nora (events).' } }, { team_loaded: true });
  assert.match(html, /Held by the contact ledger\.<\/strong> Invited to &lt;b&gt;Games night/);
  assert.doesNotMatch(html, /copy-call-draft|Open the email/);
  assert.match(html, /<div class="draft-body">Hi Ada<\/div>/);  // the draft stays readable
  vm.runInContext('ui.calls = CALLS', Object.assign(h, { CALLS: { as_of: '2026-09-15', source: 'Invented', calls: [
    { ...reach, gate: { state: 'check_first', reason: 'Confirm it is them.' } }] } }));
  assert.match(h.callsPage().split('<article')[0], /<span class="badge suppressed">Confirm first<\/span>/);
});

test('the events page escapes what people said, names who holds whom, and sends nothing', () => {
  const h = callsHarness();
  const event = { id: 'e1', name: 'Games <night>', day: '2026-09-24', city: 'New York', format: 'salon', capacity: 40, hosts: [{ name: 'Nora' }] };
  h.data = {
    source: 'Invented people', missing: null, past: [],
    next: {
      event, said_yes: 3, estimate: null, invited: [],
      invite: [{ subject_id: 'X1', name: 'Ada <b>Lin</b>', action: 'reach_now', call: 'Reach out now', why: ['Lives in New York'], talk_about: ['world models'],
        why_them: 'A paper on <i>world models</i> on Sep 8. The engine says reach out now.', links: [{ label: 'X', url: 'https://x.com/ada' }, { label: 'Bad', url: 'javascript:alert(1)' }],
        from: 'Dana <Kest>', tie: "Dana coauthored 'A <b>paper</b>'", hint: null, follow_up: null, draft: { subject: 'Night', body: 'Hey Ada, <script>x</script>' } },
        { subject_id: 'X7', name: 'Fay', action: null, call: 'Nothing to go on yet', why: [], talk_about: [], why_them: 'A researcher.', links: [], from: 'Nora', tie: null, hint: 'Omar and they were both at <Acme>: ask Omar whether they know them', follow_up: null, draft: { subject: 'Night', body: 'Hey Fay' } },
        { subject_id: 'X8', name: 'Gus', action: null, call: 'Nothing to go on yet', why: [], talk_about: [], why_them: 'Came before.', links: [], from: 'Nora', tie: 'Nora talked with them at Old salon', hint: null, follow_up: 'Old <salon>', draft: null }],
      senders: ['Nora', 'Dana <Kest>', 'Omar'],
      held: [{ subject_id: 'X3', name: 'Theo', reason: 'Works at a GI partner per Omar (sales).' }],
      briefs: [{ host: 'Nora', people: [{ subject_id: 'X1', name: 'Ada <b>Lin</b>', action: 'reach_now', call: 'Reach out now', talk_about: [], rule: 'Talk about their work. No pitch.', why_them: 'Coming.', links: [] }],
        if_invited: [{ subject_id: 'X6', name: 'Eve', action: 'verify_first', call: 'Check first', talk_about: ['world models'], rule: 'No pitch.', why_them: 'Said <b>available</b>.', links: [] }] }],
    },
    last: {
      event: { ...event, id: 'e0', day: '2026-09-10' },
      people: [{ subject_id: 'X2', name: 'Bo', action: null, call: 'Nothing to go on yet', note: null }],
      follow_ups: [
        { subject_id: 'X2', name: 'Bo', note: '<i>keen</i>', host: 'Nora', due: '2026-09-12T23:00:00+00:00', overdue: true, draft: { subject: 'Hi', body: 'Hi Bo' }, sent: null, hold: null },
        { subject_id: 'X4', name: 'Cy', note: 'later', host: 'Nora', due: '2026-09-12T23:00:00+00:00', overdue: false, draft: { subject: 'Hi', body: 'Hi Cy' }, sent: null, hold: 'Sent on 2026-09-11 by Omar (sales).' },
        { subject_id: 'X5', name: 'Di', note: 'keen', host: 'Nora', due: '2026-09-12T23:00:00+00:00', overdue: false, draft: { subject: 'Hi', body: 'Hi Di' }, sent: { by: 'Nora', at: '2026-09-11' }, hold: null },
        { subject_id: 'X8', name: 'Gus', note: 'keen', host: 'Nora', due: '2026-09-12T23:00:00+00:00', overdue: false, draft: { subject: 'Hi', body: 'Hi Gus' }, sent: null, hold: null, invites_to: 'Games <night>', invites_to_id: 'e1"x' },
      ],
    },
  };
  vm.runInContext('ui.events = data;', h);
  const html = h.eventsPage();
  assert.match(html, /Ada &lt;b&gt;Lin/);
  assert.match(html, /Games &lt;night&gt;/);
  assert.match(html, /&lt;i&gt;keen/);
  assert.doesNotMatch(html, /<b>Lin|<i>keen/);
  assert.match(html, /Theo<\/strong>: Works at a GI partner per Omar \(sales\)/);
  assert.match(html, /Overdue/);
  assert.equal(html.match(/copy-follow-up/g).length, 2); // Cy's is held and Di's went out: Bo's and Gus's are to send
  assert.equal(html.match(/data-action="event-sent"/g).length, 3); // Gus's: with the invite, or without it
  assert.match(html, /Its draft also invites them to Games &lt;night&gt;: marking it records both/);
  assert.match(html, /Mark as followed up and invited<\/button>/);
  assert.match(html, /data-invite="e1&quot;x"/);
  assert.match(html, /Followed up by Nora on/);
  assert.match(html, /Mark as followed up/);
  assert.match(html, /how did the chats go\?/);
  assert.match(html, /Open to talk now/);
  assert.match(html, /A paper on &lt;i&gt;world models&lt;\/i&gt; on Sep 8/);
  assert.match(html, /href="https:\/\/x\.com\/ada"/);
  assert.doesNotMatch(html, /javascript:/);
  assert.match(html, /Suggested, not invited yet/);
  assert.match(html, /Said &lt;b&gt;available/);
  assert.match(html, /class="door-note" data-event="e0" data-subject="X2"/);
  assert.doesNotMatch(html, />Send|data-action="send/);
  // Each invite: from the GI person who knows them, the tie on file or none claimed, a draft to copy, never sent.
  assert.match(html, /From Dana &lt;Kest&gt;:<\/strong> Dana coauthored &#39;A &lt;b&gt;paper/);
  assert.match(html, /From Nora:<\/strong> nobody at GI knows them yet, so the note claims no tie/);
  assert.match(html, /Hey Ada, &lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|id="invite-by"/);
  assert.equal(html.match(/data-action="copy-invite"/g).length, 2); // Gus's invite rides in his follow-up: no second note
  assert.match(html, /Their follow-up from Old &lt;salon&gt; is due, and its draft below now invites them too/);
  assert.match(html, /Omar and they were both at &lt;Acme&gt;: ask Omar/);
  assert.equal(html.match(/<select name="by">/g).length, 2); // Gus is marked on his follow-up, which carries the invite
  assert.match(html, /<option selected>Dana &lt;Kest&gt;<\/option>/); // the note's sender, changeable
});

test('the Monday brief ranks decisions, escapes them, links the draft, and keeps the detail behind them', () => {
  const h = callsHarness();
  h.data = {
    source: 'Invented people', missing: null, as_of: '2026-09-15', week_of: '2026-09-14', preview: 'https://app.slack.com/block-kit-builder#x',
    decisions: [
      { area: 'Events', title: 'Send Bo <x> their follow-up', why: 'They <i>talked</i>', where: 'Events', due: null, overdue: true, way_in: null, cost: null, act: null },
      { area: 'Hiring', title: 'Reach Ada <b>Lin</b> for MTS', why: 'A paper.', where: "Today's calls", due: '2026-10-19', overdue: false, way_in: 'Intro through a teammate: <Dana>', cost: 'About $312,500 a first year if hired', act: { label: 'Open the draft (email)', url: 'https://mail.example/compose?to=a' } },
      { area: 'Hiring', title: 'Check Evil', why: 'x', where: "Today's calls", due: null, overdue: false, way_in: null, cost: null, act: { label: 'bad', url: 'javascript:alert(1)' } },
    ],
    roles: [{ id: 'mts', title: 'MTS <i>', pay: 'No posted range on file', more: 2, check_first: [{ name: 'Lena', why: 'said <b>available</b>' }],
      reach: [{ subject_id: 'X1', name: 'Ada <b>Lin</b>', why: 'a paper', note: 'a note that leads with their <work>' }], held: [{ name: 'Theo', reason: 'Works at a GI partner per Omar (sales).' }] }],
    ending: [], follow_ups: [{ name: 'Bo <x>', from: 'Nora', due: null, overdue: true }],
    event: { id: 'e1', name: 'Games <night>', day: '2026-09-24', seats_left: 9, invite: [{ name: 'Cy', call: 'Reach out now' }], held: 3 },
    budget: { period: 'Q3 2026', amount: 9000, spent: 6100.5, planned: [{ name: 'Games <night>', mid: 3117, to_come: 3117 }], left: -217.5 },
  };
  vm.runInContext('ui.week = data; ui.mode = "simulation";', h);
  const html = h.weekPage();
  assert.match(html, /a paper <em>\(a note that leads with their &lt;work&gt;\)<\/em>/);
  assert.match(html, /Ada &lt;b&gt;Lin/);
  assert.match(html, /MTS &lt;i&gt;/);
  assert.match(html, /said &lt;b&gt;available/);
  assert.match(html, /Games &lt;night&gt;/);
  assert.match(html, /Bo &lt;x&gt;<\/strong>, from Nora \(overdue\)/);
  assert.doesNotMatch(html, /<b>Lin|<i>|<night>|<x>/);
  assert.match(html, /No posted range on file/);
  assert.match(html, /and 2 more on Today's calls, past this week's cap/);
  assert.match(html, /Theo<\/strong>: Works at a GI partner/);
  assert.match(html, /No wait ends this week\./);
  assert.match(html, /\$6,101 of \$9,000 spent, \$3,117 still to come for Games &lt;night&gt;; <strong>\$218 over<\/strong>/);
  assert.match(html, /href="https:\/\/app\.slack\.com\/block-kit-builder#x"/);
  assert.doesNotMatch(html, /data-action="send|>Send/);
  assert.match(html, /3 things worth your attention/);
  assert.match(html, /<strong>1\. Send Bo &lt;x&gt; their follow-up<\/strong><span>.*Overdue/);
  assert.match(html, /2\. Reach Ada &lt;b&gt;Lin&lt;\/b&gt; for MTS<\/strong><span>.*By /);
  assert.match(html, /<strong>Way in:<\/strong> Intro through a teammate: &lt;Dana&gt;/);
  assert.match(html, /About \$312,500 a first year if hired · <a href="https:\/\/mail\.example\/compose\?to=a"/);
  assert.doesNotMatch(html, /javascript:/);
  assert.match(html, /<details class="week-detail"><summary>The week in detail/);
  vm.runInContext('ui.week = { ...data, decisions: [] };', h);
  assert.match(h.weekPage(), /Nothing to decide this week.*The brief stays silent/);
});

test('the hiring budget escapes names, warns when over, and shows the role cost, not anyone pay', () => {
  const h = callsHarness();
  h.data = {
    source: 'Invented people', missing: null, invented: true, sources: { outreach: "GI's own outreach", agency: 'An agency' },
    assumptions: { year: 2027, yearly_budget: 100, overhead_pct: 25, agency_fee_pct: 20, outreach_cost: 0, conversations_per_hire: 3, salary_estimates: {}, note: '<b>n</b>' },
    roles: [{ id: 'mts', title: 'MTS <i>', offices: ['New <York>'], pay: 'No posted range on file', posted: false, salary: 200, basis: 'your estimate', estimate: 200, first_year: 250, hiring_cost: {} }],
    people: [{ subject_id: 'X1', name: 'Ada <b>Lin</b>', role_id: 'mts', role_title: 'MTS <i>', action: 'reach_now', headline: 'Reach out now', why: 'a <script>', first_year: 250, planned: false }],
    event_cost: null,
    problem: 'The saved plan <at> could not be read', payroll: null, payroll_path: 'research/private/hiring/payroll.csv',
    projection: { lines: [{ index: 0, role_id: 'mts', subject_id: null, name: null, count: 2, start_month: 3, months: 10, cost: 150, needs: null, role_title: 'MTS <i>', source_label: 'An agency' }, { index: 1, role_id: 'mts', subject_id: 'X2', name: 'Bo <img src=x>', count: 1, start_month: 1, months: 12, cost: null, needs: 'an event cost', role_title: 'MTS <i>', source_label: 'A GI event' }], by_role: [{ title: 'MTS <i>', hires: 3, cost: 150, uncosted: 1 }], total: 150, budget: 100, left: -50, over: true, hires: 3, missing: [1], uncosted_hires: 1, run_rate: 300, hires_cost: 150, payroll: 0, offices: [{ office: 'New <York>', now: 0, planned: 3, desks: 3 }] },
  };
  vm.runInContext('ui.hiring = data; ui.mode = "simulation";', h);
  const html = h.hiringPage();
  assert.match(html, /Ada &lt;b&gt;Lin/);
  assert.match(html, /MTS &lt;i&gt;/);
  assert.match(html, /a &lt;script&gt;/);
  assert.match(html, /&lt;b&gt;n&lt;\/b&gt;/);
  assert.match(html, /Bo &lt;img src=x&gt;/);
  assert.match(html, /The saved plan &lt;at&gt; could not be read/);
  assert.doesNotMatch(html, /<b>Lin|<i>|<script>|<b>n|<img|<at>/);
  assert.match(html, /Over budget by \$50\.<\/strong> Push a start month back, drop a line, or raise the budget\. Not counting 1 hire that can't be costed yet\./);
  assert.match(html, /needs an event cost/);
  assert.match(html, /MTS &lt;i&gt;: 3 hires, \$150 in 2027 \(1 not costed yet\)/);
  assert.match(html, /find the hire another way than a GI event/);
  assert.match(html, /Salary estimate, \$ a year \(invented for the demo\)/);
  assert.match(html, /No payroll export on file, so this counts new hires only\./);
  assert.match(html, /<td>New &lt;York&gt;<\/td><td>0<\/td><td>3<\/td><td>3<\/td>/);
  assert.match(html, /2 open seats/);
  assert.match(html, /About <strong>\$250<\/strong> in a first year/);
  assert.match(html, /never a guess at what anyone earns now/);
  assert.match(html, /A GI event: no past event has a qualified conversation yet/);
});

test('new starts escape what ops typed, show owners and due days, and offer skipping only what was asked', () => {
  const h = callsHarness();
  const item = (key, label, extra = {}) => ({ key, label, owner: 'Kai', state: 'open', note: '', asked: false, due: '2026-09-28', overdue: false, ...extra });
  h.data = {
    as_of: '2026-09-15', problem: null, owners: ['Kai', 'Rae <x>'], note: '',
    states: { ask: 'Ask at the offer', open: 'To do', done: 'Done', not_needed: 'Not needed' },
    roles: [{ id: 'backend', title: 'Backend', offices: ['New York City'] }],
    from_plan: [{ subject_id: 'A1', name: 'Rin <i>Vale</i>', role_id: 'backend', office: 'New York City', start: '2027-01-01' }],
    hires: [{ id: 'w1', name: 'Wren <b>E</b>', role_id: 'backend', office: 'New York City', start: '2026-10-05', offer_on: '2026-09-08', open: 2, started: false,
      items: [item('visa', 'Visa', { asked: true, state: 'ask', note: 'Said: <script>x</script>' }), item('desk', 'Office and desk', { overdue: true, due: '2026-09-10' }), item('laptop', 'Laptop and accounts', { state: 'done' })] }],
  };
  vm.runInContext('ui.starts = data;', h);
  const html = h.startsPage();
  assert.match(html, /Wren &lt;b&gt;E/);
  assert.match(html, /Rin &lt;i&gt;Vale/);
  assert.match(html, /value="Said: &lt;script&gt;x&lt;\/script&gt;"/);
  assert.doesNotMatch(html, /<script>|<b>E|<i>Vale/);
  assert.match(html, /Rae &lt;x&gt;/);
  assert.match(html, /Overdue since/);
  assert.match(html, /2 open/);
  const visa = html.slice(html.indexOf('data-key="visa"'), html.indexOf('data-key="desk"'));
  const desk = html.slice(html.indexOf('data-key="desk"'), html.indexOf('data-key="laptop"'));
  assert.match(visa, /Not needed/);
  assert.match(visa, /What the candidate said at the offer stage/);
  assert.doesNotMatch(desk, /Not needed|Ask at the offer|offer stage/); // always needed: nothing to skip, nothing asked
  assert.match(html, /Add an accepted offer/);
  assert.doesNotMatch(html.slice(0, html.indexOf('Add an accepted offer')), /type="submit"/); // each change saves itself
  assert.match(html, /<details class="evidence-item"><summary class="row"><span><strong>Laptop and accounts/); // done: one line
  assert.match(html, /data-name="Wren &lt;b&gt;E&lt;\/b&gt;"/); // for the confirm before Remove
});

test('the monthly one-pager escapes names, shows zeros quietly, and marks a budget over', () => {
  const h = callsHarness();
  h.data = {
    missing: null, month: 'September 2026', as_of: '2026-09-15', source: 'Invented people', invented: true, text: 'x',
    year: 2027, headline: '2 things slipping · people <u>', plan_unreadable: false, footnote: 'Reached counts notes, never Slack cards <b>',
    pipeline: [{ role_id: 'r', title: 'Role <b>1</b>', to_reach: 3, reached: 0, replied: 0, active: 2, offers: 1, filled: 1, planned: 2, starting: 1 },
      { role_id: 's', title: 'Other', to_reach: 1, reached: 1, replied: 0, active: 0, offers: 0, filled: 0, planned: 0, starting: 0 }],
    people_budget: { year: 2027, budget: 100, total: 150, left: -50, payroll: 90, hires: 1, uncosted: 0 },
    event_budget: null,
    slipping: [{ title: 'Get <i>Wren</i> ready', why: 'Open: <script>x</script>' }, { title: 'Thin pipeline', why: null }],
  };
  vm.runInContext('ui.month = data;', h);
  const html = h.monthPage();
  assert.match(html, /Role &lt;b&gt;1/);
  assert.match(html, /Get &lt;i&gt;Wren&lt;\/i&gt; ready<\/strong>: Open: &lt;script&gt;/);
  assert.doesNotMatch(html, /<script>|<b>1|<i>Wren/);
  assert.match(html, /<strong>\$50 over<\/strong>/);
  assert.match(html, /1 planned hire;?[^s]/);
  assert.match(html, /<span class="muted">0<\/span>/);
  assert.match(html, /Every person and number here is invented/);
  assert.match(html, /data-action="month-copy"/);
  assert.match(html, /<strong>2 things slipping · people &lt;u&gt;<\/strong>/); // the headline, escaped
  assert.match(html, /<th>2027 hires filled<\/th>/);
  assert.match(html, /<td>1 of 2<\/td>/);
  assert.match(html, /none planned/);
  assert.match(html, /never Slack cards &lt;b&gt;/);
});

test('a change on one ops page makes every other ops page load afresh, and leaves the one on screen', () => {
  const h = harness('hiring');
  const cached = { calls: 1, week: 1, hiring: 1, starts: 1, month: 1, events: 1, scorecard: 1, monthError: 'old' };
  Object.assign(h.context.ui, cached);
  h.context.opsChanged();
  const ui = h.context.ui;
  assert.deepEqual([ui.calls, ui.week, ui.starts, ui.month, ui.events], [null, null, null, null, null]);
  assert.equal(ui.monthError, '');  // an old error would stop the page loading again
  assert.equal(ui.hiring, 1);  // the page on screen reloads itself
  assert.equal(ui.scorecard, 1);  // the scorecard reads none of it
  h.context.opsChanged(true);  // a reset: the page on screen too
  assert.equal(ui.hiring, null);
});
