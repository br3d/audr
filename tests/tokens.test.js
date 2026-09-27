'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { generateResetToken, hashToken } = require('../src/auth/tokens');

test('generateResetToken returns a random raw token and its sha-256 hash', () => {
  const a = generateResetToken();
  const b = generateResetToken();
  assert.match(a.raw, /^[0-9a-f]{64}$/, 'raw is 32 random bytes hex');
  assert.notStrictEqual(a.raw, b.raw, 'tokens are unique');
  assert.strictEqual(a.hash, hashToken(a.raw), 'hash matches the raw token');
  assert.notStrictEqual(a.hash, a.raw, 'the stored value is the hash, not the raw token');
});
