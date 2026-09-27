'use strict';

/**
 * Test preload (node --require ./tests/setup.js). Runs before any application
 * module is loaded, so auth config picks up a hermetic test environment:
 * an in-memory database, a fixed session secret, and cheap bcrypt rounds.
 * These are non-secret test fixtures, committed so `npm test` works in CI
 * without any local .env file.
 */
process.env.NODE_ENV = process.env.NODE_ENV || 'test';
process.env.AUTH_DB_PATH = process.env.AUTH_DB_PATH || ':memory:';
process.env.SESSION_SECRET = process.env.SESSION_SECRET || 'test-secret-0123456789abcdef';
process.env.APP_URL = process.env.APP_URL || 'http://localhost:3000';
process.env.BCRYPT_ROUNDS = process.env.BCRYPT_ROUNDS || '4';
