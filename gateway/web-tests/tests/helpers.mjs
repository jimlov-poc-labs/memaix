// SPDX-License-Identifier: AGPL-3.0-or-later
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { vi } from 'vitest';

const here = path.dirname(fileURLToPath(import.meta.url));
export const STATIC = path.resolve(here, '../../src/memaix_gateway/web/static');
const PAGES = path.resolve(here, '../../src/memaix_gateway/web/pages');

let loads = 0;

export async function loadApp() {
  await import(/* @vite-ignore */ path.join(STATIC, 'app.js'));
}

// Put a page's markup (without its <script> tag) into the document.
export function mountPage(name) {
  const html = fs.readFileSync(path.join(PAGES, `${name}.html`), 'utf8');
  document.body.innerHTML = html.replace(/<script[\s\S]*?<\/script>/g, '');
}

// Fresh evaluation of a page module (pages run at import time).
export async function runPage(name) {
  loads += 1;
  await import(/* @vite-ignore */ `${path.join(STATIC, `${name}.js`)}?run=${loads}`);
}

// fetch mock: routes is { 'GET /app/api/x': body | (url, opts) => Response-like }.
export function mockFetch(routes) {
  const calls = [];
  const fn = vi.fn(async (url, opts = {}) => {
    const key = `${(opts.method || 'GET').toUpperCase()} ${String(url).split('?')[0]}`;
    calls.push({ key, url: String(url), opts });
    const route = routes[key];
    if (route === undefined) return jsonResponse({ error: `unmocked ${key}` }, 404);
    return typeof route === 'function' ? route(String(url), opts) : jsonResponse(route);
  });
  vi.stubGlobal('fetch', fn);
  fn.calls = calls;
  return fn;
}

export function jsonResponse(body, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    statusText: String(status),
    json: async () => body,
  };
}

export const flush = () => new Promise((r) => setTimeout(r, 0));
