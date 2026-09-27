'use strict';

const path = require('path');
const fs = require('fs');
const crypto = require('crypto');
const Database = require('better-sqlite3');

/**
 * Data layer for the auth system.
 *
 * Everything the rest of the app knows about persistence goes through this
 * module, so swapping SQLite for Postgres later is a localized change: keep
 * the exported repository functions, replace the driver underneath.
 *
 * SQLite (file-backed) is deliberate for the foundation — zero external infra
 * to run locally or in CI, WAL mode for concurrent reads, and it is trivially
 * exportable. The DB path is configurable via AUTH_DB_PATH so tests can point
 * at a throwaway file and production can point at a mounted volume.
 */

let db;

function resolveDbPath() {
  const configured = process.env.AUTH_DB_PATH;
  if (configured === ':memory:') return ':memory:';
  const target = configured
    ? path.resolve(configured)
    : path.join(process.cwd(), 'data', 'app.db');
  fs.mkdirSync(path.dirname(target), { recursive: true });
  return target;
}

function migrate(database) {
  database.pragma('journal_mode = WAL');
  database.pragma('foreign_keys = ON');
  database.exec(`
    CREATE TABLE IF NOT EXISTS users (
      id            TEXT PRIMARY KEY,
      email         TEXT NOT NULL UNIQUE,
      password_hash TEXT NOT NULL,
      created_at    INTEGER NOT NULL,
      updated_at    INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS password_reset_tokens (
      id         TEXT PRIMARY KEY,
      user_id    TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
      token_hash TEXT NOT NULL UNIQUE,
      expires_at INTEGER NOT NULL,
      used_at    INTEGER,
      created_at INTEGER NOT NULL
    );

    CREATE INDEX IF NOT EXISTS idx_reset_tokens_user ON password_reset_tokens(user_id);
  `);
}

/**
 * Lazily open (and memoize) the database connection. Callers that need a fresh
 * database in tests should set AUTH_DB_PATH and call resetForTests().
 */
function getDb() {
  if (!db) {
    db = new Database(resolveDbPath());
    migrate(db);
  }
  return db;
}

// ---- User repository -------------------------------------------------------

function createUser({ email, passwordHash }) {
  const now = Date.now();
  const id = crypto.randomUUID();
  getDb()
    .prepare(
      `INSERT INTO users (id, email, password_hash, created_at, updated_at)
       VALUES (@id, @email, @passwordHash, @now, @now)`
    )
    .run({ id, email, passwordHash, now });
  return findUserById(id);
}

function findUserByEmail(email) {
  return getDb()
    .prepare('SELECT * FROM users WHERE email = ?')
    .get(email);
}

function findUserById(id) {
  return getDb().prepare('SELECT * FROM users WHERE id = ?').get(id);
}

function updateUserEmail(id, email) {
  getDb()
    .prepare('UPDATE users SET email = ?, updated_at = ? WHERE id = ?')
    .run(email, Date.now(), id);
  return findUserById(id);
}

function updateUserPassword(id, passwordHash) {
  getDb()
    .prepare('UPDATE users SET password_hash = ?, updated_at = ? WHERE id = ?')
    .run(passwordHash, Date.now(), id);
  return findUserById(id);
}

function deleteUser(id) {
  getDb().prepare('DELETE FROM users WHERE id = ?').run(id);
}

// ---- Password-reset token repository --------------------------------------

function createResetToken({ userId, tokenHash, expiresAt }) {
  const id = crypto.randomUUID();
  getDb()
    .prepare(
      `INSERT INTO password_reset_tokens (id, user_id, token_hash, expires_at, created_at)
       VALUES (?, ?, ?, ?, ?)`
    )
    .run(id, userId, tokenHash, expiresAt, Date.now());
  return id;
}

function findResetToken(tokenHash) {
  return getDb()
    .prepare('SELECT * FROM password_reset_tokens WHERE token_hash = ?')
    .get(tokenHash);
}

function markResetTokenUsed(id) {
  getDb()
    .prepare('UPDATE password_reset_tokens SET used_at = ? WHERE id = ?')
    .run(Date.now(), id);
}

/** Invalidate any outstanding reset tokens for a user (e.g. after a reset). */
function deleteResetTokensForUser(userId) {
  getDb()
    .prepare('DELETE FROM password_reset_tokens WHERE user_id = ?')
    .run(userId);
}

// ---- Test helpers ----------------------------------------------------------

function resetForTests() {
  if (db) {
    db.close();
    db = undefined;
  }
}

module.exports = {
  getDb,
  createUser,
  findUserByEmail,
  findUserById,
  updateUserEmail,
  updateUserPassword,
  deleteUser,
  createResetToken,
  findResetToken,
  markResetTokenUsed,
  deleteResetTokensForUser,
  resetForTests,
};
