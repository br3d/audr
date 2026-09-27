'use strict';

const session = require('express-session');
const SqliteStore = require('better-sqlite3-session-store')(session);
const db = require('../db');
const { config } = require('./config');

/**
 * Session middleware.
 *
 * Sessions are server-side: the browser only holds an opaque, signed session
 * id in an httpOnly cookie, so the actual session data (and the logged-in user
 * id) never leaves the server. The store is the same SQLite database used for
 * users, so sessions survive process restarts and the session persists across
 * page reloads. Expired sessions are swept periodically.
 */
function buildSessionMiddleware() {
  // In tests we use express-session's default in-memory store: it needs no
  // external state and, unlike the SQLite store's cleanup timer, leaves no
  // lingering interval to keep the test runner's event loop alive.
  const store = config.nodeEnv === 'test'
    ? undefined
    : new SqliteStore({
        client: db.getDb(),
        expired: { clear: true, intervalMs: 15 * 60 * 1000 },
      });

  return session({
    store,
    name: 'sid',
    secret: config.sessionSecret,
    resave: false,
    saveUninitialized: false,
    rolling: true, // refresh the cookie's max-age on activity
    cookie: {
      httpOnly: true, // not readable by JS -> mitigates XSS token theft
      sameSite: 'lax', // CSRF defense-in-depth alongside the csrf token
      secure: config.isProd, // only sent over HTTPS in production
      maxAge: 7 * 24 * 60 * 60 * 1000, // 7 days
    },
  });
}

module.exports = { buildSessionMiddleware };
