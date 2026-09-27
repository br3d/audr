'use strict';

const { test, beforeEach } = require('node:test');
const assert = require('node:assert');
const request = require('supertest');

const db = require('../src/db');
const { createApp } = require('../src/app');
const { captureConsole, extractResetUrl, extractCsrf, tokenFromUrl } = require('./helpers');

function freshApp() {
  db.resetForTests();
  return createApp();
}

/** GET a page with the agent and return its CSRF token (also primes the session cookie). */
async function csrfFor(agent, path) {
  const res = await agent.get(path);
  return extractCsrf(res.text);
}

beforeEach(() => {
  db.resetForTests();
});

test('health and root endpoints work without a session', async () => {
  const app = freshApp();
  const health = await request(app).get('/health');
  assert.strictEqual(health.status, 200);
  assert.strictEqual(health.body.status, 'ok');
  assert.strictEqual(health.headers['set-cookie'], undefined, 'health sets no session cookie');

  const root = await request(app).get('/');
  assert.strictEqual(root.status, 200);
});

test('unauthenticated access to a protected route redirects to /login', async () => {
  const app = freshApp();
  const res = await request(app).get('/dashboard');
  assert.strictEqual(res.status, 302);
  assert.match(res.headers.location, /^\/login\?next=/);
});

test('sign up establishes a session that persists across requests, then logout ends it', async () => {
  const app = freshApp();
  const agent = request.agent(app);

  const token = await csrfFor(agent, '/signup');
  assert.ok(token, 'signup page exposes a CSRF token');

  const signup = await agent
    .post('/signup')
    .type('form')
    .send({ _csrf: token, email: 'gina@example.com', password: 'goodpass1' });
  assert.strictEqual(signup.status, 302, 'signup redirects on success');
  assert.strictEqual(signup.headers.location, '/account');

  // Session persists: a fresh request with the same agent (cookie) is still authed.
  const account = await agent.get('/account');
  assert.strictEqual(account.status, 200);
  assert.match(account.text, /gina@example\.com/);

  const dashboard = await agent.get('/dashboard');
  assert.strictEqual(dashboard.status, 200, 'protected route now accessible');

  // Logout requires a fresh CSRF token from the post-login session.
  const postLoginToken = await csrfFor(agent, '/account');
  const logout = await agent.post('/logout').type('form').send({ _csrf: postLoginToken });
  assert.strictEqual(logout.status, 302);

  const afterLogout = await agent.get('/dashboard');
  assert.strictEqual(afterLogout.status, 302, 'protected route blocked after logout');
});

test('login flow authenticates a registered user', async () => {
  const app = freshApp();
  // Seed a user directly through the service.
  const service = require('../src/auth/service');
  await service.signUp('hank@example.com', 'goodpass1');

  const agent = request.agent(app);
  const token = await csrfFor(agent, '/login');
  const login = await agent
    .post('/login')
    .type('form')
    .send({ _csrf: token, email: 'hank@example.com', password: 'goodpass1' });
  assert.strictEqual(login.status, 302);
  assert.strictEqual(login.headers.location, '/account');

  const bad = request.agent(app);
  const badToken = await csrfFor(bad, '/login');
  const badLogin = await bad
    .post('/login')
    .type('form')
    .send({ _csrf: badToken, email: 'hank@example.com', password: 'wrongpass1' });
  assert.strictEqual(badLogin.status, 401, 'wrong password is rejected');
});

test('state-changing POST without a CSRF token is rejected with 403', async () => {
  const app = freshApp();
  const agent = request.agent(app);
  await agent.get('/login'); // establish a session so we reach the CSRF check
  const res = await agent
    .post('/login')
    .type('form')
    .send({ email: 'x@example.com', password: 'whatever1' }); // no _csrf
  assert.strictEqual(res.status, 403);
});

test('end-to-end password reset over HTTP lets the user log in with the new password', async () => {
  const app = freshApp();
  const service = require('../src/auth/service');
  await service.signUp('ivy@example.com', 'goodpass1');

  const agent = request.agent(app);

  // Request a reset and capture the emailed link from the dev mailer log.
  const forgotToken = await csrfFor(agent, '/forgot-password');
  const lines = await captureConsole(async () => {
    await agent
      .post('/forgot-password')
      .type('form')
      .send({ _csrf: forgotToken, email: 'ivy@example.com' });
  });
  const url = extractResetUrl(lines);
  assert.ok(url, 'reset link was produced');
  const rawToken = tokenFromUrl(url);

  // Visit the reset page (gets a CSRF token) and submit a new password.
  const resetAgent = request.agent(app);
  const resetPageToken = await csrfFor(resetAgent, `/reset-password?token=${rawToken}`);
  const reset = await resetAgent
    .post('/reset-password')
    .type('form')
    .send({ _csrf: resetPageToken, token: rawToken, password: 'brandnew123' });
  assert.strictEqual(reset.status, 200);
  assert.match(reset.text, /Password updated/);

  // The new password now works end-to-end.
  const loginAgent = request.agent(app);
  const loginToken = await csrfFor(loginAgent, '/login');
  const login = await loginAgent
    .post('/login')
    .type('form')
    .send({ _csrf: loginToken, email: 'ivy@example.com', password: 'brandnew123' });
  assert.strictEqual(login.status, 302, 'login with the new password succeeds');
});
