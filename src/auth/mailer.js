'use strict';

const nodemailer = require('nodemailer');
const { config } = require('./config');

/**
 * Email delivery for auth flows.
 *
 * If SMTP is configured (SMTP_HOST/USER/PASS), we send real mail. Otherwise we
 * fall back to a JSON transport that logs the message — so local development
 * and CI work with zero email infrastructure, and the reset link is visible in
 * the server logs. The link itself is never emailed to anyone but the account
 * owner in production.
 */

let cachedTransport;

function getTransport() {
  if (cachedTransport) return cachedTransport;
  if (config.smtp.host && config.smtp.user) {
    cachedTransport = nodemailer.createTransport({
      host: config.smtp.host,
      port: config.smtp.port,
      secure: config.smtp.port === 465,
      auth: { user: config.smtp.user, pass: config.smtp.pass },
    });
  } else {
    // Dev/CI fallback: does not send, returns the composed message as JSON.
    cachedTransport = nodemailer.createTransport({ jsonTransport: true });
  }
  return cachedTransport;
}

async function sendPasswordResetEmail(email, resetUrl) {
  const transport = getTransport();
  const info = await transport.sendMail({
    from: config.smtp.from,
    to: email,
    subject: 'Reset your password',
    text: `We received a request to reset your password.\n\nUse this link to choose a new password (valid for a limited time):\n${resetUrl}\n\nIf you did not request this, you can safely ignore this email.`,
    html: `<p>We received a request to reset your password.</p><p><a href="${resetUrl}">Reset your password</a> (link valid for a limited time).</p><p>If you did not request this, you can safely ignore this email.</p>`,
  });

  // When using the JSON fallback there is no real delivery — surface the link
  // so a developer can complete the flow locally.
  if (!config.smtp.host) {
    console.log(
      JSON.stringify({ level: 'info', msg: 'password_reset_email_dev', to: email, resetUrl })
    );
  }
  return info;
}

/** Reset the memoized transport (used by tests). */
function resetTransportForTests() {
  cachedTransport = undefined;
}

module.exports = { sendPasswordResetEmail, resetTransportForTests };
