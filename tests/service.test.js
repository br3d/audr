'use strict';

const { test, beforeEach } = require('node:test');
const assert = require('node:assert');

const db = require('../src/db');
const service = require('../src/auth/service');
const { generateResetToken } = require('../src/auth/tokens');
const { captureConsole, extractResetUrl, tokenFromUrl } = require('./helpers');

beforeEach(() => {
  db.resetForTests(); // fresh in-memory database per test
});

test('signUp creates a user and rejects duplicates and weak input', async () => {
  const user = await service.signUp('Alice@Example.com', 'goodpass1');
  assert.strictEqual(user.email, 'alice@example.com', 'email is normalized');
  assert.ok(user.id, 'user has an id');

  await assert.rejects(() => service.signUp('alice@example.com', 'goodpass1'), /already registered/);
  await assert.rejects(() => service.signUp('not-an-email', 'goodpass1'), /valid email/);
  await assert.rejects(() => service.signUp('bob@example.com', 'weak'), /at least 8/);
});

test('authenticate accepts correct credentials and rejects wrong ones', async () => {
  await service.signUp('bob@example.com', 'goodpass1');
  const ok = await service.authenticate('bob@example.com', 'goodpass1');
  assert.strictEqual(ok.email, 'bob@example.com');

  await assert.rejects(() => service.authenticate('bob@example.com', 'wrongpass1'), /Incorrect/);
  await assert.rejects(() => service.authenticate('nobody@example.com', 'goodpass1'), /Incorrect/);
});

test('changeEmail and changePassword enforce their rules', async () => {
  const user = await service.signUp('carol@example.com', 'goodpass1');
  await service.signUp('taken@example.com', 'goodpass1');

  await assert.rejects(() => service.changeEmail(user.id, 'taken@example.com'), /already in use/);
  const updated = await service.changeEmail(user.id, 'carol2@example.com');
  assert.strictEqual(updated.email, 'carol2@example.com');

  await assert.rejects(
    () => service.changePassword(user.id, 'wrongpass1', 'newpass123'),
    /current password is incorrect/
  );
  await service.changePassword(user.id, 'goodpass1', 'newpass123');
  await service.authenticate('carol2@example.com', 'newpass123'); // resolves
  await assert.rejects(() => service.authenticate('carol2@example.com', 'goodpass1'), /Incorrect/);
});

test('deleteAccount removes the user', async () => {
  const user = await service.signUp('dan@example.com', 'goodpass1');
  service.deleteAccount(user.id);
  await assert.rejects(() => service.authenticate('dan@example.com', 'goodpass1'), /Incorrect/);
});

test('password reset: request emails a link, token resets the password once', async () => {
  await service.signUp('erin@example.com', 'goodpass1');

  const lines = await captureConsole(() => service.requestPasswordReset('erin@example.com'));
  const url = extractResetUrl(lines);
  assert.ok(url, 'a reset URL was produced');
  const rawToken = tokenFromUrl(url);
  assert.ok(rawToken, 'reset URL carries a token');

  const user = await service.resetPassword(rawToken, 'brandnew123');
  assert.strictEqual(user.email, 'erin@example.com');
  await service.authenticate('erin@example.com', 'brandnew123'); // new password works
  await assert.rejects(() => service.authenticate('erin@example.com', 'goodpass1'), /Incorrect/);

  // Token is single-use.
  await assert.rejects(() => service.resetPassword(rawToken, 'another123'), /invalid or has expired/);
});

test('password reset: request for unknown email is a silent no-op', async () => {
  const lines = await captureConsole(() => service.requestPasswordReset('ghost@example.com'));
  assert.strictEqual(extractResetUrl(lines), null, 'no email sent for unknown accounts');
});

test('password reset: expired tokens are rejected', async () => {
  const user = await service.signUp('frank@example.com', 'goodpass1');
  const { raw, hash } = generateResetToken();
  db.createResetToken({ userId: user.id, tokenHash: hash, expiresAt: Date.now() - 1000 });
  await assert.rejects(() => service.resetPassword(raw, 'brandnew123'), /invalid or has expired/);
});
