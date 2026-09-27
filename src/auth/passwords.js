'use strict';

const bcrypt = require('bcryptjs');
const { config } = require('./config');

/**
 * Password hashing and policy.
 *
 * We use bcrypt (via bcryptjs, a pure-JS implementation with no native build
 * step) rather than hand-rolling any crypto. Hashes are salted per-password by
 * bcrypt itself; plain-text passwords are never stored or logged.
 */

const MIN_LENGTH = 8;
const MAX_LENGTH = 200; // bcrypt truncates at 72 bytes; cap input to avoid DoS on huge bodies.

/**
 * Validate a password against the policy. Returns an array of human-readable
 * error strings (empty when valid).
 */
function validatePassword(password) {
  const errors = [];
  if (typeof password !== 'string' || password.length < MIN_LENGTH) {
    errors.push(`Password must be at least ${MIN_LENGTH} characters.`);
  }
  if (typeof password === 'string' && password.length > MAX_LENGTH) {
    errors.push(`Password must be at most ${MAX_LENGTH} characters.`);
  }
  if (typeof password === 'string' && !/[a-zA-Z]/.test(password)) {
    errors.push('Password must contain at least one letter.');
  }
  if (typeof password === 'string' && !/[0-9]/.test(password)) {
    errors.push('Password must contain at least one number.');
  }
  return errors;
}

async function hashPassword(password) {
  return bcrypt.hash(password, config.bcryptRounds);
}

async function verifyPassword(password, hash) {
  if (!hash) return false;
  return bcrypt.compare(password, hash);
}

module.exports = { validatePassword, hashPassword, verifyPassword, MIN_LENGTH };
