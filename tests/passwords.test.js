'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const { validatePassword, hashPassword, verifyPassword } = require('../src/auth/passwords');

test('validatePassword enforces length and composition', () => {
  assert.ok(validatePassword('short1').length > 0, 'too short is rejected');
  assert.ok(validatePassword('allletters').length > 0, 'no number is rejected');
  assert.ok(validatePassword('12345678').length > 0, 'no letter is rejected');
  assert.deepStrictEqual(validatePassword('goodpass1'), [], 'valid password passes');
});

test('hashPassword produces a bcrypt hash that verifies', async () => {
  const hash = await hashPassword('goodpass1');
  assert.match(hash, /^\$2[aby]\$/, 'looks like a bcrypt hash');
  assert.notStrictEqual(hash, 'goodpass1', 'never stores plaintext');
  assert.strictEqual(await verifyPassword('goodpass1', hash), true);
  assert.strictEqual(await verifyPassword('wrongpass1', hash), false);
});

test('verifyPassword returns false for a missing hash', async () => {
  assert.strictEqual(await verifyPassword('whatever1', null), false);
});
