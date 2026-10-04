// SPDX-License-Identifier: AGPL-3.0-or-later
import { afterEach, beforeEach, vi } from 'vitest';

// Page modules attach listeners to window/document that would otherwise leak
// into the next test, so track and remove them after each test.
// Listeners added in beforeAll (app.js loading once) are kept for the whole file.
const tracked = [];
let recording = false;
beforeEach(() => { recording = true; });
for (const target of [window, document]) {
  const add = target.addEventListener.bind(target);
  target.addEventListener = (type, fn, opts) => {
    if (recording) tracked.push([target, type, fn, opts]);
    add(type, fn, opts);
  };
}

afterEach(() => {
  for (const [target, type, fn, opts] of tracked.splice(0)) target.removeEventListener(type, fn, opts);
  vi.unstubAllGlobals();
  document.body.innerHTML = '';
  document.documentElement.removeAttribute('data-theme');
  localStorage.clear();
  history.replaceState(null, '', '/');
  delete window.ME;
  delete window.I18N;
});
