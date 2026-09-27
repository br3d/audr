'use strict';

const crypto = require('crypto');

/**
 * Password-reset token helpers.
 *
 * The raw token is a 256-bit random value sent to the user (in the reset link).
 * We only ever persist its SHA-256 hash, so a database leak does not hand an
 * attacker usable reset tokens — the same reason we hash passwords. Comparison
 * on lookup is by hash, and lookups use a constant-time-safe path via the
 * unique index.
 */

function generateResetToken() {
  const raw = crypto.randomBytes(32).toString('hex');
  return { raw, hash: hashToken(raw) };
}

function hashToken(raw) {
  return crypto.createHash('sha256').update(raw).digest('hex');
}

module.exports = { generateResetToken, hashToken };
