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

function get(url, headers) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const req = https.request({ hostname: u.hostname, path: u.pathname + u.search, method: 'GET', headers }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch { resolve(d); } });
    });
    req.on('error', reject);
    req.end();
  });
}

function post(url, headers, body) {
  return new Promise((resolve, reject) => {
    const u = new URL(url);
    const s = JSON.stringify(body);
    const req = https.request({
      hostname: u.hostname, path: u.pathname + u.search, method: 'POST',
      headers: { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(s), ...headers },
    }, res => {
      let d = '';
      res.on('data', c => d += c);
      res.on('end', () => { try { resolve(JSON.parse(d)); } catch { resolve(d); } });
    });
    req.on('error', reject);
    req.write(s);
    req.end();
  });
}

async function fetchNewIssues() {
  const auth = Buffer.from(`${SONAR_TOKEN}:`).toString('base64');
  const url = `${SONAR_HOST}/api/issues/search?projectKeys=${PROJECT_KEY}&resolved=false&severities=BLOCKER,CRITICAL,MAJOR&sinceLeakPeriod=true&ps=50`;
  const data = await get(url, { Authorization: `Basic ${auth}` });
  return data.issues || [];
}

async function analyzeWithClaude(issues) {
  const summary = issues.map(i => {
    const file = (i.component || '').split(':').pop();
    const line = i.textRange?.startLine;
    return `[${i.severity}] ${i.rule}: ${i.message} (${file}${line ? ':' + line : ''})`;
  }).join('\n');
  const res = await post('https://api.anthropic.com/v1/messages', {
    'x-api-key': ANTHROPIC_KEY,
    'anthropic-version': '2023-06-01',
  }, {
    model: 'claude-haiku-4-5-20251001',
    max_tokens: 300,
    messages: [{ role: 'user', content: `SonarQube flagged these new issues in ${PROJECT_NAME}. In 2-3 sentences, explain what needs fixing and why it matters. Be direct.\n\n${summary}` }],
  });
  return res.content?.[0]?.text || '(no analysis)';
}

async function notifyDiscord(issues, analysis) {
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
  if (issues.length > 6) lines.push(`_…and ${issues.length - 6} more_`);
  const content = [
    `${top} **SonarQube · ${PROJECT_NAME}** — ${counts.BLOCKER} blocker · ${counts.CRITICAL} critical · ${counts.MAJOR} major`,
    '',
    lines.join('\n'),
    '',
    `> ${analysis}`,
    '',
    `commit \`${COMMIT_SHA}\` · <${SONAR_HOST}/project/issues?id=${PROJECT_KEY}&sinceLeakPeriod=true>`,
  ].join('\n').slice(0, 2000);
  await post(`https://discord.com/api/v10/channels/${DISCORD_CHANNEL}/messages`, {
    Authorization: `Bot ${DISCORD_TOKEN}`,
  }, { content });
}

(async () => {
  const issues = await fetchNewIssues();
  if (issues.length === 0) { console.log('No new issues.'); process.exit(0); }
  console.log(`${issues.length} new issue(s) found.`);
  const analysis = await analyzeWithClaude(issues);
  await notifyDiscord(issues, analysis);
  console.log('Discord notified.');
  if (issues.some(i => i.severity === 'BLOCKER')) process.exit(1);
})().catch(e => { console.error(e.message); process.exit(1); });
