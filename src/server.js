'use strict';

const { createApp } = require('./app');

// The host injects PORT (Render, Railway, Fly all do this). Default for local.
const port = Number(process.env.PORT) || 3000;

const app = createApp();

const server = app.listen(port, () => {
  // Structured, secret-free startup log.
  console.log(
    JSON.stringify({
      level: 'info',
      msg: 'server_started',
      port,
      environment: process.env.APP_ENV || 'development',
      version: process.env.GIT_SHA || 'dev',
    })
  );
});

// Graceful shutdown so the platform can roll deploys without dropping traffic.
function shutdown(signal) {
  console.log(JSON.stringify({ level: 'info', msg: 'shutdown', signal }));
  server.close(() => process.exit(0));
  // Failsafe if connections hang.
  setTimeout(() => process.exit(1), 10000).unref();
}

process.on('SIGTERM', () => shutdown('SIGTERM'));
process.on('SIGINT', () => shutdown('SIGINT'));

module.exports = { server };
