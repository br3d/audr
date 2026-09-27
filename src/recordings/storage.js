'use strict';

/**
 * Audio storage adapter — Cloudflare R2.
 *
 * Choice rationale: R2 is S3-compatible, has zero egress fees, and suits a
 * self-hosted single-owner app where storage cost and operational complexity
 * should be minimal. Render's ephemeral disk is unsuitable for durable audio
 * storage. R2 exposes the standard S3 API so we use @aws-sdk/client-s3.
 *
 * Required env vars:
 *   R2_ACCOUNT_ID        — Cloudflare account ID (hex)
 *   R2_ACCESS_KEY_ID     — R2 API token ID
 *   R2_SECRET_ACCESS_KEY — R2 API token secret
 *   R2_BUCKET            — bucket name
 *   R2_PUBLIC_URL        — optional: public domain for read URLs
 *                          (e.g. https://assets.example.com). When absent,
 *                          presigned S3 URLs are returned instead.
 */

const { S3Client, PutObjectCommand, GetObjectCommand } = require('@aws-sdk/client-s3');
const { getSignedUrl } = require('@aws-sdk/s3-request-presigner');

let _client = null;

function client() {
  if (_client) return _client;
  const accountId = requireEnv('R2_ACCOUNT_ID');
  _client = new S3Client({
    region: 'auto',
    endpoint: `https://${accountId}.r2.cloudflarestorage.com`,
    credentials: {
      accessKeyId: requireEnv('R2_ACCESS_KEY_ID'),
      secretAccessKey: requireEnv('R2_SECRET_ACCESS_KEY'),
    },
  });
  return _client;
}

function requireEnv(name) {
  const v = process.env[name];
  if (!v) throw new Error(`Missing required env var: ${name}`);
  return v;
}

function bucket() {
  return requireEnv('R2_BUCKET');
}

/**
 * Upload audio bytes to R2.
 * @param {string} key        — object key, e.g. `recordings/<uuid>.webm`
 * @param {Buffer} buffer     — raw audio bytes
 * @param {string} mimeType   — Content-Type header value
 */
async function upload(key, buffer, mimeType) {
  await client().send(
    new PutObjectCommand({
      Bucket: bucket(),
      Key: key,
      Body: buffer,
      ContentType: mimeType,
    })
  );
  return key;
}

/**
 * Return a URL to read the object.
 * Uses the public domain when R2_PUBLIC_URL is set, otherwise a presigned URL
 * valid for the given number of seconds (default 3600).
 * @param {string} key
 * @param {number} [expiresIn=3600]
 */
async function getReadUrl(key, expiresIn = 3600) {
  const publicBase = process.env.R2_PUBLIC_URL;
  if (publicBase) {
    return `${publicBase.replace(/\/$/, '')}/${key}`;
  }
  return getSignedUrl(
    client(),
    new GetObjectCommand({ Bucket: bucket(), Key: key }),
    { expiresIn }
  );
}

/** Flush the cached S3 client (useful in tests that swap env vars). */
function resetForTests() {
  _client = null;
}

module.exports = { upload, getReadUrl, resetForTests };
