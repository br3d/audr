'use strict';

const db = require('../db');
const { toPublicUser } = require('./service');

/**
 * Auth middleware.
 *
 * loadUser resolves the logged-in user (if any) from the session on every
 * request and exposes it as req.user and res.locals.currentUser (for views).
 * requireAuth guards protected routes; requireGuest keeps logged-in users off
 * the login/signup pages.
 */

function loadUser(req, res, next) {
  const userId = req.session && req.session.userId;
  if (userId) {
    const row = db.findUserById(userId);
    if (row) {
      req.user = toPublicUser(row);
    } else {
      // Session points at a deleted user — clear it.
      req.session.userId = undefined;
    }
  }
  res.locals.currentUser = req.user || null;
  next();
}

function requireAuth(req, res, next) {
  if (req.user) return next();
  // Preserve where the user was heading so we can bounce them back post-login.
  const returnTo = encodeURIComponent(req.originalUrl || '/');
  if (req.accepts(['html', 'json']) === 'json') {
    return res.status(401).json({ error: 'Authentication required.' });
  }
  return res.redirect(`/login?next=${returnTo}`);
}

function requireGuest(req, res, next) {
  if (req.user) return res.redirect('/account');
  next();
}

module.exports = { loadUser, requireAuth, requireGuest };
