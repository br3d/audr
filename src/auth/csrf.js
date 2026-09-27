'use strict';

const { csrfSync } = require('csrf-sync');

/**
 * CSRF protection using the synchroniser-token pattern (token stored in the
 * server-side session, echoed back in a hidden form field / header and checked
 * on unsafe requests). This pairs with SameSite=lax cookies for defense in
 * depth. Because it relies on the session, the session middleware must run
 * first.
 */
const {
  csrfSynchronisedProtection,
  generateToken,
} = csrfSync({
  // Pull the token from the form body (server-rendered forms) or a header
  // (fetch/XHR clients).
  getTokenFromRequest: (req) =>
    (req.body && req.body._csrf) || req.headers['x-csrf-token'],
});

/**
 * Expose a per-request CSRF token to views via res.locals.csrfToken, so every
 * rendered form can drop it into a hidden input without threading it manually.
 */
function csrfTokenLocals(req, res, next) {
  res.locals.csrfToken = generateToken(req);
  next();
}

module.exports = { csrfSynchronisedProtection, generateToken, csrfTokenLocals };
