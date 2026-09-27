'use strict';

/**
 * Auth configuration, resolved from the environment with safe local defaults.
 *
 * The single source of truth for what needs to be set in production lives in
 * .env.example. Anything security-sensitive (session secret, secure cookies)
 * fails loud or degrades safely when NODE_ENV=production.
 */

const nodeEnv = process.env.NODE_ENV || 'development';
const isProd = nodeEnv === 'production';

function resolveSessionSecret() {
  const secret = process.env.SESSION_SECRET;
  if (secret && secret.length >= 16) return secret;
  if (isProd) {
    throw new Error(
      'SESSION_SECRET must be set to a strong random value (>=16 chars) in production.'
    );
  }
  // Dev/test only: stable per-process fallback so sessions survive reloads
  // during local development. Never used in production (guarded above).
  return 'dev-insecure-session-secret-change-me';
}

const config = {
  nodeEnv,
  isProd,
  sessionSecret: resolveSessionSecret(),
  // Public base URL, used to build absolute password-reset links.
  appUrl: (process.env.APP_URL || `http://localhost:${process.env.PORT || 3000}`).replace(/\/$/, ''),
  // Password reset token time-to-live.
  resetTokenTtlMs: Number(process.env.RESET_TOKEN_TTL_MS) || 60 * 60 * 1000, // 1h
  // Cost factor for bcrypt.
  bcryptRounds: Number(process.env.BCRYPT_ROUNDS) || 12,
  // SMTP (optional). When unset, the mailer logs the reset link to stdout.
  smtp: {
    host: process.env.SMTP_HOST,
    port: Number(process.env.SMTP_PORT) || 587,
    user: process.env.SMTP_USER,
    pass: process.env.SMTP_PASS,
    from: process.env.MAIL_FROM || 'no-reply@onboarding-saas.local',
  },
};

module.exports = { config };
