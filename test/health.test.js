'use strict';

const test = require('node:test');
const assert = require('node:assert');
const { createApp } = require('../src/app');

// Start the app on an ephemeral port, hit an endpoint, return parsed JSON.
async function request(path) {
  const app = createApp();
  const server = app.listen(0);
  await new Promise((resolve) => server.once('listening', resolve));
  const { port } = server.address();
  try {
    const res = await fetch(`http://127.0.0.1:${port}${path}`);
    const body = await res.json();
    return { status: res.status, body };
  } finally {
    server.close();
  }
}

test('GET /health returns 200 with status ok', async () => {
  const { status, body } = await request('/health');
  assert.strictEqual(status, 200);
  assert.strictEqual(body.status, 'ok');
  assert.ok('uptimeSeconds' in body);
});

test('GET / returns 200', async () => {
  const { status } = await request('/');
  assert.strictEqual(status, 200);
});
