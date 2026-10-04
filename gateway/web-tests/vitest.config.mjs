// SPDX-License-Identifier: AGPL-3.0-or-later
import { defineConfig } from 'vitest/config';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const here = path.dirname(fileURLToPath(import.meta.url));
const STATIC = path.resolve(here, '../src/memaix_gateway/web/static');
const GLOBALS = ['api', 't', 'toast', 'modal', 'mdView', 'mdInline', 'pollBadge', 'relTime'];

// app.js is a classic <script> whose top-level functions are page globals.
// In tests it is imported as a module, so publish them on globalThis.
const publishAppGlobals = {
  name: 'publish-app-globals',
  transform(code, id) {
    if (id.split('?')[0] !== path.join(STATIC, 'app.js')) return null;
    return { code: `${code}\nObject.assign(globalThis, { ${GLOBALS.join(', ')} });\n`, map: null };
  },
};

export default defineConfig({
  plugins: [publishAppGlobals],
  server: { fs: { strict: false } },
  test: {
    environment: 'jsdom',
    include: ['tests/**/*.test.mjs'],
    setupFiles: ['tests/setup.mjs'],
    restoreMocks: true,
    coverage: {
      provider: 'v8',
      include: [`${STATIC}/*.js`],
      allowExternal: true,
      reporter: ['text-summary', 'lcov'],
      reportsDirectory: 'coverage',
    },
  },
});
