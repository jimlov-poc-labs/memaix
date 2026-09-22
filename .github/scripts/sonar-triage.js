#!/usr/bin/env node
// Fetches new SonarQube issues after a scan, calls Claude for analysis, posts to Discord.
// Env: SONAR_TOKEN, ANTHROPIC_API_KEY, DISCORD_BOT_TOKEN, DISCORD_CHANNEL_ID,
//      PROJECT_KEY, PROJECT_NAME, GITHUB_SHA, SONAR_HOST_URL
const https = require('https');

const SONAR_HOST = process.env.SONAR_HOST_URL || 'https://sonar.jimlov.se';
const SONAR_TOKEN = process.env.SONAR_TOKEN;
const ANTHROPIC_KEY = process.env.ANTHROPIC_API_KEY;
const DISCORD_TOKEN = process.env.DISCORD_BOT_TOKEN;
const DISCORD_CHANNEL = process.env.DISCORD_CHANNEL_ID;
const PROJECT_KEY = process.env.PROJECT_KEY;
const PROJECT_NAME = process.env.PROJECT_NAME || PROJECT_KEY;
const COMMIT_SHA = (process.env.GITHUB_SHA || '').slice(0, 7);

function request(url, method, headers, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const s = body === undefined ? undefined : JSON.stringify(body);
    const h = s === undefined ? headers
      : { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(s), ...headers };
    const req = https.request({ hostname: u.hostname, path: u.pathname + u.search, method, headers: h }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => {
        let parsed = d;
        try { parsed = JSON.parse(d); } catch { /* not JSON, keep text */ }
        resolve({ status: res.statusCode, body: parsed });
      });
    });
    req.on('error', reject);
    if (s !== undefined) req.write(s);
    req.end();
  });
}

async function fetchNewIssues() {
  const auth = Buffer.from(`${SONAR_TOKEN}:`).toString('base64');
  // components=, not projectKeys=: this server silently ignores projectKeys and
  // returns every project's issues, which looks like an answer but is not one.
  const url = `${SONAR_HOST}/api/issues/search?components=${encodeURIComponent(PROJECT_KEY)}` +
    '&resolved=false&severities=BLOCKER,CRITICAL,MAJOR&sinceLeakPeriod=true&ps=100';
  const { status, body } = await request(url, 'GET', { Authorization: `Basic ${auth}` });
  // A failed call must not read as "no new issues".
  if (status !== 200 || !Array.isArray(body.issues)) {
    throw new Error(`Sonar issues/search failed: HTTP ${status}`);
  }
  const foreign = body.issues.filter(i => i.project !== PROJECT_KEY);
  if (foreign.length) {
    throw new Error(`Sonar returned ${foreign.length} issue(s) from other projects -- filter ignored`);
  }
  return { issues: body.issues, total: body.total ?? body.paging?.total ?? body.issues.length };
}

async function analyzeWithClaude(issues) {
  const summary = issues.map(i => {
    const file = (i.component || '').split(':').pop();
    const line = i.textRange?.startLine;
    return `[${i.severity}] ${i.rule}: ${i.message} (${file}${line ? ':' + line : ''})`;
  }).join('\n');
  const { status, body } = await request('https://api.anthropic.com/v1/messages', 'POST', {
    'x-api-key': ANTHROPIC_KEY,
    'anthropic-version': '2023-06-01',
  }, {
    model: 'claude-haiku-4-5-20251001',
    max_tokens: 300,
    messages: [{ role: 'user', content: `SonarQube flagged these new issues in ${PROJECT_NAME}. In 2-3 sentences, explain what needs fixing and why it matters. Be direct.\n\n${summary}` }],
  });
  const text = body?.content?.[0]?.text;
  if (text) return text;
  const why = body?.error ? `${body.error.type}: ${body.error.message}` : `HTTP ${status}`;
  console.error(`Claude analysis failed: ${why}`);
  return `(analysis failed -- ${why})`;
}

async function notifyDiscord(issues, total, analysis) {
  const counts = { BLOCKER: 0, CRITICAL: 0, MAJOR: 0 };
  issues.forEach(i => { if (i.severity in counts) counts[i.severity]++; });
  const emo = { BLOCKER: '🚨', CRITICAL: '🔴', MAJOR: '🟡' };
  const top = counts.BLOCKER > 0 ? '🚨' : counts.CRITICAL > 0 ? '🔴' : '🟡';
  const lines = issues.slice(0, 6).map(i => {
    const file = (i.component || '').split(':').pop();
    const line = i.textRange?.startLine;
    const msg = i.message.length > 70 ? i.message.slice(0, 70) + '…' : i.message;
    return `${emo[i.severity] || '⚪'} \`${i.rule}\` — ${msg} *(${file}${line ? ':' + line : ''})*`;
  });
  if (total > 6) lines.push(`_…and ${total - 6} more_`);
  const partial = total > issues.length ? ` (counts from first ${issues.length} of ${total})` : '';
  const content = [
    `${top} **SonarQube · ${PROJECT_NAME}** — ${counts.BLOCKER} blocker · ${counts.CRITICAL} critical · ${counts.MAJOR} major${partial}`,
    '',
    lines.join('\n'),
    '',
    `> ${analysis}`,
    '',
    `commit \`${COMMIT_SHA}\` · <${SONAR_HOST}/project/issues?id=${PROJECT_KEY}&sinceLeakPeriod=true>`,
  ].join('\n').slice(0, 2000);
  const { status } = await request(`https://discord.com/api/v10/channels/${DISCORD_CHANNEL}/messages`, 'POST', {
    Authorization: `Bot ${DISCORD_TOKEN}`,
  }, { content });
  if (status < 200 || status >= 300) throw new Error(`Discord post failed: HTTP ${status}`);
}

(async () => {
  const { issues, total } = await fetchNewIssues();
  if (issues.length === 0) { console.log('No new issues.'); process.exit(0); }
  console.log(`${total} new issue(s) found.`);
  const analysis = await analyzeWithClaude(issues);
  await notifyDiscord(issues, total, analysis);
  console.log('Discord notified.');
  if (issues.some(i => i.severity === 'BLOCKER')) process.exit(1);
})().catch(e => { console.error(e.message); process.exit(1); });
