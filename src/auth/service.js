'use strict';

const db = require('../db');
const { config } = require('./config');
const passwords = require('./passwords');
const { generateResetToken, hashToken } = require('./tokens');
const { sendPasswordResetEmail } = require('./mailer');

/**
 * Auth business logic, independent of Express. Routes call into here; tests can
 * exercise the full sign-up / login / reset lifecycle without HTTP.
 *
 * Errors that are safe to show a user are thrown as AuthError with a `.code`.
 */

class AuthError extends Error {
  constructor(code, message) {
    super(message);
    this.name = 'AuthError';
    this.code = code;
  }
}

const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

function normalizeEmail(email) {
  return typeof email === 'string' ? email.trim().toLowerCase() : '';
}

function assertValidEmail(email) {
  if (!EMAIL_RE.test(email) || email.length > 254) {
    throw new AuthError('invalid_email', 'Please enter a valid email address.');
  }
}

function toPublicUser(row) {
  if (!row) return null;
  return { id: row.id, email: row.email, createdAt: row.created_at };
}

async function signUp(rawEmail, password) {
  const email = normalizeEmail(rawEmail);
  assertValidEmail(email);

  const pwErrors = passwords.validatePassword(password);
  if (pwErrors.length) {
    throw new AuthError('weak_password', pwErrors.join(' '));
  }

  if (db.findUserByEmail(email)) {
    // Do not reveal whether an email is registered beyond this generic message.
    throw new AuthError('email_taken', 'That email is already registered.');
  }

  const passwordHash = await passwords.hashPassword(password);
  const user = db.createUser({ email, passwordHash });
  return toPublicUser(user);
}

/**
 * Verify credentials. Always runs a bcrypt comparison (even for unknown
 * emails, against a real dummy hash) to avoid leaking account existence via
 * timing. The dummy hash is computed once at load with the configured cost so
 * the "user not found" path costs the same as a wrong password.
 */
const DUMMY_HASH = require('bcryptjs').hashSync('unused-timing-equalizer', config.bcryptRounds);

async function authenticate(rawEmail, password) {
  const email = normalizeEmail(rawEmail);
  const user = db.findUserByEmail(email);
  const ok = await passwords.verifyPassword(password, user ? user.password_hash : DUMMY_HASH);
  if (!user || !ok) {
    throw new AuthError('invalid_credentials', 'Incorrect email or password.');
  }
  return toPublicUser(user);
}

async function changeEmail(userId, rawEmail) {
  const email = normalizeEmail(rawEmail);
  assertValidEmail(email);
  const existing = db.findUserByEmail(email);
  if (existing && existing.id !== userId) {
    throw new AuthError('email_taken', 'That email is already in use.');
  }
  return toPublicUser(db.updateUserEmail(userId, email));
}

async function changePassword(userId, currentPassword, newPassword) {
  const user = db.findUserById(userId);
  if (!user) throw new AuthError('not_found', 'Account not found.');

  const ok = await passwords.verifyPassword(currentPassword, user.password_hash);
  if (!ok) {
    throw new AuthError('invalid_credentials', 'Your current password is incorrect.');
  }
  const pwErrors = passwords.validatePassword(newPassword);
  if (pwErrors.length) {
    throw new AuthError('weak_password', pwErrors.join(' '));
  }
  const passwordHash = await passwords.hashPassword(newPassword);
  db.updateUserPassword(userId, passwordHash);
  // Any outstanding reset tokens are no longer valid once the password changes.
  db.deleteResetTokensForUser(userId);
}

function deleteAccount(userId) {
  db.deleteUser(userId); // cascades to reset tokens via FK.
}

/**
 * Begin a password reset. Always resolves successfully regardless of whether
 * the email exists — never reveals account existence. Sends an email with a
 * one-time link when the account is real.
 */
async function requestPasswordReset(rawEmail) {
  const email = normalizeEmail(rawEmail);
  const user = db.findUserByEmail(email);
  if (!user) return; // Silent no-op: do not disclose non-existence.

  const { raw, hash } = generateResetToken();
  const expiresAt = Date.now() + config.resetTokenTtlMs;
  db.createResetToken({ userId: user.id, tokenHash: hash, expiresAt });

  const resetUrl = `${config.appUrl}/reset-password?token=${raw}`;
  await sendPasswordResetEmail(user.email, resetUrl);
}

/**
 * Complete a password reset using the raw token from the emailed link.
 * Validates existence, single-use, and expiry.
 */
async function resetPassword(rawToken, newPassword) {
  if (typeof rawToken !== 'string' || rawToken.length < 16) {
    throw new AuthError('invalid_token', 'This reset link is invalid.');
  }
  const record = db.findResetToken(hashToken(rawToken));
  if (!record || record.used_at || record.expires_at < Date.now()) {
    throw new AuthError('invalid_token', 'This reset link is invalid or has expired.');
  }
  const pwErrors = passwords.validatePassword(newPassword);
  if (pwErrors.length) {
    throw new AuthError('weak_password', pwErrors.join(' '));
  }
  const passwordHash = await passwords.hashPassword(newPassword);
  db.updateUserPassword(record.user_id, passwordHash);
  db.markResetTokenUsed(record.id);
  // Invalidate any other outstanding tokens for this user.
  db.deleteResetTokensForUser(record.user_id);
  return toPublicUser(db.findUserById(record.user_id));
}

module.exports = {
  AuthError,
  signUp,
  authenticate,
  changeEmail,
  changePassword,
  deleteAccount,
  requestPasswordReset,
  resetPassword,
  toPublicUser,
  normalizeEmail,
};
