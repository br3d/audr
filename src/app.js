'use strict';

const path = require('path');
const express = require('express');

const { buildSessionMiddleware } = require('./auth/session');
const { csrfSynchronisedProtection, csrfTokenLocals } = require('./auth/csrf');
const { loadUser, requireAuth } = require('./auth/middleware');
const authRouter = require('./routes/auth');
const { config } = require('./auth/config');

/**
 * Build the Express application.
 *
 * Kept separate from server startup so tests can import the app without
 * binding a port.
 */
function createApp() {
  const app = express();

  app.disable('x-powered-by');

  // Server-rendered auth/account pages.
  app.set('view engine', 'ejs');
  app.set('views', path.join(__dirname, 'views'));

  // Behind a TLS-terminating proxy in production, trust it so secure cookies
  // and req.ip (used for rate-limiting) work correctly.
  if (config.isProd) app.set('trust proxy', 1);

  // Body parsers: JSON for API clients, urlencoded for the HTML forms.
  app.use(express.json());
  app.use(express.urlencoded({ extended: false }));

  // Health check: returns 200 whenever the process is up and able to serve.
  // Deliberately dependency-free and unauthenticated so load balancers and
  // the platform health probe can hit it cheaply. Registered before the auth
  // pipeline so probes never create sessions or CSRF tokens.
  app.get('/health', (_req, res) => {
    res.status(200).json({
      status: 'ok',
      service: process.env.SERVICE_NAME || 'saas-app',
      // APP_ENV is set per-environment (staging/production) by the host.
      environment: process.env.APP_ENV || 'development',
      // Surface the deployed commit for quick "what's live?" checks.
      version: process.env.GIT_SHA || 'dev',
      uptimeSeconds: Math.round(process.uptime()),
    });
  });

  app.get('/', (_req, res) => {
    res.status(200).json({ message: 'saas-app is running. See /health.' });
  });

  // --- Auth pipeline --------------------------------------------------------
  // Order matters: session -> resolve current user -> issue CSRF token ->
  // enforce CSRF on unsafe methods. All must precede the auth routes.
  app.use(buildSessionMiddleware());
  app.use(loadUser);
  app.use(csrfTokenLocals); // exposes res.locals.csrfToken for every view
  app.use(csrfSynchronisedProtection); // no-op on GET/HEAD/OPTIONS

  // Auth + account management routes (signup/login/logout/reset/account).
  app.use('/', authRouter);

  // Example protected route demonstrating requireAuth middleware.
  app.get('/dashboard', requireAuth, (req, res) => {
    res.render('dashboard', { user: req.user });
  });

  // CSRF failures surface as a clean 403 rather than a stack trace.
  app.use((err, req, res, next) => {
    if (err && err.code === 'EBADCSRFTOKEN') {
      return res.status(403).render('message', {
        title: 'Request blocked',
        heading: 'Security check failed',
        body: 'Your session may have expired. Please go back and try again.',
        link: { href: '/login', label: 'Back to sign in' },
        currentUser: req.user || null,
      });
    }
    return next(err);
  });

  return app;
}

module.exports = { createApp };
