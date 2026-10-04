// SPDX-License-Identifier: AGPL-3.0-or-later
// Sonar resolves lcov paths from the repo root; vitest writes them relative to web-tests/.
import fs from 'node:fs';

const file = new URL('../coverage/lcov.info', import.meta.url);
fs.writeFileSync(file, fs.readFileSync(file, 'utf8').replace(/^SF:\.\.\//gm, 'SF:gateway/'));
