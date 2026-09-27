'use strict';

/**
 * Test helpers shared across the auth suite.
 */

/** Run `fn` while capturing console.log output; returns the collected lines. */
async function captureConsole(fn) {
  const lines = [];
  const original = console.log;
  console.log = (...args) => lines.push(args.join(' '));
  try {
    await fn();
  } finally {
    console.log = original;
  }
  return lines;
}

/** Pull the reset URL the dev mailer logs (password_reset_email_dev). */
function extractResetUrl(lines) {
  for (const line of lines) {
    try {
      const parsed = JSON.parse(line);
      if (parsed.msg === 'password_reset_email_dev') return parsed.resetUrl;
    } catch {
      /* not JSON, ignore */
    }
  }
  return null;
}

/** Extract the hidden CSRF token value from a rendered HTML page. */
function extractCsrf(html) {
  const m = html.match(/name="_csrf" value="([^"]+)"/);
  return m ? m[1] : null;
}

/** Extract the `token` query param from a reset URL. */
function tokenFromUrl(url) {
  return new URL(url).searchParams.get('token');
}

module.exports = { captureConsole, extractResetUrl, extractCsrf, tokenFromUrl };
