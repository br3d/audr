'use strict';

const express = require('express');

/**
 * Build the Express application.
 *
 * Kept separate from server startup so tests can import the app without
 * binding a port.
 */
function createApp() {
  const app = express();

  app.disable('x-powered-by');
  app.use(express.json());

  // Health check: returns 200 whenever the process is up and able to serve.
  // Deliberately dependency-free and unauthenticated so load balancers and
  // the platform health probe can hit it cheaply.
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

  return app;
}

module.exports = { createApp };
