'use strict';

// Build/verify step for the app.
//
// This is a plain Node.js (CommonJS) service — there is no transpile/bundle
// step. "Build" here means a fast, dependency-free production-readiness check
// so CI fails early on a broken require graph or missing entrypoint. Extend
// this when a real build (bundling, asset compilation) is introduced.

const fs = require('fs');
const path = require('path');

const root = path.resolve(__dirname, '..');
const required = ['src/server.js', 'src/app.js', 'package.json'];

let failed = false;

for (const rel of required) {
  const abs = path.join(root, rel);
  if (!fs.existsSync(abs)) {
    console.error(`build: missing required file: ${rel}`);
    failed = true;
  }
}

// Smoke-check that the app module loads and exposes createApp.
try {
  const { createApp } = require(path.join(root, 'src/app.js'));
  if (typeof createApp !== 'function') {
    console.error('build: src/app.js does not export createApp()');
    failed = true;
  } else {
    // Ensure the app actually constructs without throwing.
    createApp();
  }
} catch (err) {
  console.error(`build: failed to load application: ${err.message}`);
  failed = true;
}

if (failed) {
  process.exit(1);
}

// Emit a build manifest so deploys can record exactly what shipped.
const pkg = require(path.join(root, 'package.json'));
const distDir = path.join(root, 'dist');
fs.mkdirSync(distDir, { recursive: true });

const manifest = {
  name: pkg.name,
  version: pkg.version,
  gitSha: process.env.GIT_SHA || 'dev',
  node: process.version,
  builtAt: new Date().toISOString(),
  entry: pkg.main || 'src/server.js',
};
fs.writeFileSync(
  path.join(distDir, 'build-manifest.json'),
  JSON.stringify(manifest, null, 2) + '\n'
);

console.log(JSON.stringify({ level: 'info', msg: 'build_ok', ...manifest }));
