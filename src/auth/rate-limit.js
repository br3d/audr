'use strict';

const rateLimit = require('express-rate-limit');

/**
 * Rate limiters for sensitive auth endpoints, to blunt credential-stuffing and
 * password-reset abuse. This uses the default in-memory store, which is fine
 * for a single instance; a multi-instance deployment should back it with Redis
 * (express-rate-limit supports a shared store) — flagged in .env.example.
 */

const disabled = process.env.NODE_ENV === 'test'; // don't throttle the test suite

function make({ windowMs, limit, message }) {
  return rateLimit({
    windowMs,
    limit,
    standardHeaders: 'draft-7',
    legacyHeaders: false,
    skip: () => disabled,
    // Key by client IP + submitted email so one attacker can't lock out a
    // victim's account across the whole IP pool, and vice versa.
    keyGenerator: (req) => `${req.ip}:${(req.body && req.body.email) || ''}`,
    handler: (req, res) => {
      const wantsJson = req.accepts(['html', 'json']) === 'json';
      if (wantsJson) return res.status(429).json({ error: message });
      return res.status(429).render('message', {
        title: 'Too many attempts',
        heading: 'Too many attempts',
        body: message,
      });
    },
  });
}

// 10 login attempts / 15 min / (ip+email).
const loginLimiter = make({
  windowMs: 15 * 60 * 1000,
  limit: 10,
  message: 'Too many login attempts. Please try again in a few minutes.',
});

// 5 reset requests / hour / (ip+email).
const resetRequestLimiter = make({
  windowMs: 60 * 60 * 1000,
  limit: 5,
  message: 'Too many password reset requests. Please try again later.',
});

module.exports = { loginLimiter, resetRequestLimiter };
