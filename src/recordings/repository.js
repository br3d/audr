'use strict';

const crypto = require('crypto');
const db = require('../db');
const { assertTransition } = require('./lifecycle');

// ---- recordings -------------------------------------------------------------

/**
 * Create a new recording row for the given user.
 * Status starts at 'uploaded'. Duration is explicitly null when unknown.
 */
function createRecording({ userId, title, originalFilename, mimeType, storageKey, durationSeconds = null }) {
  const id = crypto.randomUUID();
  const now = Date.now();
  db.getDb()
    .prepare(`
      INSERT INTO recordings
        (id, user_id, title, original_filename, mime_type, duration_seconds, status, storage_key, created_at, updated_at)
      VALUES
        (@id, @userId, @title, @originalFilename, @mimeType, @durationSeconds, 'uploaded', @storageKey, @now, @now)
    `)
    .run({ id, userId, title, originalFilename, mimeType, durationSeconds, storageKey, now });
  return findRecordingById(id, userId);
}

/** Returns null when the recording doesn't exist or belongs to a different user. */
function findRecordingById(id, userId) {
  return (
    db.getDb()
      .prepare('SELECT * FROM recordings WHERE id = ? AND user_id = ?')
      .get(id, userId) ?? null
  );
}

/** Returns the user's recordings, newest first. */
function listRecordings(userId, { limit = 50, offset = 0 } = {}) {
  return db.getDb()
    .prepare('SELECT * FROM recordings WHERE user_id = ? ORDER BY created_at DESC LIMIT ? OFFSET ?')
    .all(userId, limit, offset);
}

/**
 * Advance the lifecycle state of a recording owned by userId.
 * Throws LifecycleError for invalid transitions, { code: 'not_found' } when
 * the recording doesn't exist or belongs to another user.
 */
function transitionStatus(id, userId, to, { errorReason = null } = {}) {
  const row = findRecordingById(id, userId);
  if (!row) {
    const err = new Error('Recording not found');
    err.code = 'not_found';
    throw err;
  }
  assertTransition(row.status, to);
  const now = Date.now();
  db.getDb()
    .prepare('UPDATE recordings SET status = ?, error_reason = ?, updated_at = ? WHERE id = ? AND user_id = ?')
    .run(to, errorReason, now, id, userId);
  return findRecordingById(id, userId);
}

/** Hard-delete a recording row (and cascade to transcripts/summaries). Only affects the owner's row. */
function deleteRecording(id, userId) {
  db.getDb()
    .prepare('DELETE FROM recordings WHERE id = ? AND user_id = ?')
    .run(id, userId);
}

// ---- transcripts ------------------------------------------------------------

/**
 * Attach a transcript to a recording.
 * Segments: array of { speaker, start_ms, end_ms, text }, serialised to JSON.
 */
function createTranscript({ recordingId, provider, language = null, fullText = null, segments = null }) {
  const id = crypto.randomUUID();
  const now = Date.now();
  db.getDb()
    .prepare(`
      INSERT INTO transcripts (id, recording_id, provider, language, full_text, segments, created_at, updated_at)
      VALUES (@id, @recordingId, @provider, @language, @fullText, @segments, @now, @now)
    `)
    .run({
      id,
      recordingId,
      provider,
      language,
      fullText,
      segments: segments !== null ? JSON.stringify(segments) : null,
      now,
    });
  return db.getDb().prepare('SELECT * FROM transcripts WHERE id = ?').get(id);
}

/** Returns null when the recording doesn't exist or belongs to a different user. */
function findTranscript(recordingId, userId) {
  return (
    db.getDb()
      .prepare(`
        SELECT t.* FROM transcripts t
        JOIN recordings r ON r.id = t.recording_id
        WHERE t.recording_id = ? AND r.user_id = ?
      `)
      .get(recordingId, userId) ?? null
  );
}

// ---- summaries --------------------------------------------------------------

/**
 * Attach a summary to a recording.
 * decisions / actionItems: arrays, serialised to JSON.
 */
function createSummary({ recordingId, summaryText = null, decisions = null, actionItems = null }) {
  const id = crypto.randomUUID();
  const now = Date.now();
  db.getDb()
    .prepare(`
      INSERT INTO summaries (id, recording_id, summary_text, decisions, action_items, created_at, updated_at)
      VALUES (@id, @recordingId, @summaryText, @decisions, @actionItems, @now, @now)
    `)
    .run({
      id,
      recordingId,
      summaryText,
      decisions: decisions !== null ? JSON.stringify(decisions) : null,
      actionItems: actionItems !== null ? JSON.stringify(actionItems) : null,
      now,
    });
  return db.getDb().prepare('SELECT * FROM summaries WHERE id = ?').get(id);
}

/** Returns null when the recording doesn't exist or belongs to a different user. */
function findSummary(recordingId, userId) {
  return (
    db.getDb()
      .prepare(`
        SELECT s.* FROM summaries s
        JOIN recordings r ON r.id = s.recording_id
        WHERE s.recording_id = ? AND r.user_id = ?
      `)
      .get(recordingId, userId) ?? null
  );
}

module.exports = {
  createRecording,
  findRecordingById,
  listRecordings,
  transitionStatus,
  deleteRecording,
  createTranscript,
  findTranscript,
  createSummary,
  findSummary,
};
