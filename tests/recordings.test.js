'use strict';

const { test, beforeEach } = require('node:test');
const assert = require('node:assert');

const db = require('../src/db');
const repo = require('../src/recordings/repository');
const { isValidStatus, canTransition, assertTransition, LifecycleError, STATUSES } = require('../src/recordings/lifecycle');

// Shared user fixture across tests.
let userId;

beforeEach(() => {
  db.resetForTests();
  const user = db.createUser({ email: 'owner@example.com', passwordHash: 'x'.repeat(60) });
  userId = user.id;
});

// ---- lifecycle unit tests ---------------------------------------------------

test('STATUSES contains the five defined statuses', () => {
  assert.deepStrictEqual([...STATUSES].sort(), ['failed', 'ready', 'summarizing', 'transcribing', 'uploaded']);
});

test('isValidStatus rejects unknown values', () => {
  assert.ok(isValidStatus('uploaded'));
  assert.ok(isValidStatus('failed'));
  assert.ok(!isValidStatus('pending'));
  assert.ok(!isValidStatus(''));
  assert.ok(!isValidStatus(null));
});

test('canTransition: valid forward path and terminal states', () => {
  assert.ok(canTransition('uploaded', 'transcribing'));
  assert.ok(canTransition('transcribing', 'summarizing'));
  assert.ok(canTransition('summarizing', 'ready'));
  // failed is reachable from every non-terminal state
  assert.ok(canTransition('uploaded', 'failed'));
  assert.ok(canTransition('transcribing', 'failed'));
  assert.ok(canTransition('summarizing', 'failed'));
  assert.ok(canTransition('ready', 'failed'));
  // no reverse transitions
  assert.ok(!canTransition('ready', 'uploaded'));
  assert.ok(!canTransition('summarizing', 'uploaded'));
  // failed is terminal
  assert.ok(!canTransition('failed', 'uploaded'));
  assert.ok(!canTransition('failed', 'ready'));
});

test('assertTransition throws LifecycleError for invalid moves', () => {
  assert.throws(() => assertTransition('ready', 'uploaded'), LifecycleError);
  assert.throws(() => assertTransition('failed', 'transcribing'), LifecycleError);
  assert.throws(() => assertTransition('uploaded', 'bogus'), LifecycleError);
  assert.throws(() => assertTransition('bogus', 'uploaded'), LifecycleError);
});

test('assertTransition does not throw for valid moves', () => {
  assert.doesNotThrow(() => assertTransition('uploaded', 'transcribing'));
  assert.doesNotThrow(() => assertTransition('transcribing', 'summarizing'));
  assert.doesNotThrow(() => assertTransition('summarizing', 'ready'));
  assert.doesNotThrow(() => assertTransition('uploaded', 'failed'));
});

// ---- repository: recordings -------------------------------------------------

function makeRecording(overrides = {}) {
  return repo.createRecording({
    userId,
    title: 'Team standup',
    originalFilename: 'standup.webm',
    mimeType: 'audio/webm',
    storageKey: 'recordings/abc123.webm',
    ...overrides,
  });
}

test('createRecording persists a row with status=uploaded and null duration', () => {
  const rec = makeRecording();
  assert.ok(rec.id, 'has id');
  assert.strictEqual(rec.status, 'uploaded');
  assert.strictEqual(rec.user_id, userId);
  assert.strictEqual(rec.title, 'Team standup');
  assert.strictEqual(rec.mime_type, 'audio/webm');
  assert.strictEqual(rec.storage_key, 'recordings/abc123.webm');
  assert.strictEqual(rec.duration_seconds, null, 'unknown duration stays null');
  assert.strictEqual(rec.error_reason, null);
});

test('createRecording stores optional duration_seconds when provided', () => {
  const rec = makeRecording({ durationSeconds: 142.5 });
  assert.strictEqual(rec.duration_seconds, 142.5);
});

test('findRecordingById returns null for a different user', () => {
  const rec = makeRecording();
  const other = db.createUser({ email: 'other@example.com', passwordHash: 'y'.repeat(60) });
  const found = repo.findRecordingById(rec.id, other.id);
  assert.strictEqual(found, null, 'ownership enforced on read');
});

test('listRecordings returns only the requesting user recordings', () => {
  makeRecording({ title: 'A' });
  makeRecording({ title: 'B' });
  const other = db.createUser({ email: 'other2@example.com', passwordHash: 'y'.repeat(60) });
  repo.createRecording({ userId: other.id, title: 'C', originalFilename: 'c.mp3', mimeType: 'audio/mpeg', storageKey: 'c.mp3' });

  const mine = repo.listRecordings(userId);
  assert.strictEqual(mine.length, 2);
  assert.ok(mine.every(r => r.user_id === userId));
});

test('listRecordings returns rows in descending created_at order', () => {
  makeRecording({ title: 'First' });
  makeRecording({ title: 'Second' });
  const rows = repo.listRecordings(userId);
  assert.ok(rows[0].created_at >= rows[1].created_at, 'newest first');
});

test('transitionStatus follows valid lifecycle path', () => {
  const rec = makeRecording();
  const r1 = repo.transitionStatus(rec.id, userId, 'transcribing');
  assert.strictEqual(r1.status, 'transcribing');
  const r2 = repo.transitionStatus(r1.id, userId, 'summarizing');
  assert.strictEqual(r2.status, 'summarizing');
  const r3 = repo.transitionStatus(r2.id, userId, 'ready');
  assert.strictEqual(r3.status, 'ready');
});

test('transitionStatus to failed stores error_reason', () => {
  const rec = makeRecording();
  const r = repo.transitionStatus(rec.id, userId, 'failed', { errorReason: 'whisper timeout' });
  assert.strictEqual(r.status, 'failed');
  assert.strictEqual(r.error_reason, 'whisper timeout');
});

test('transitionStatus throws LifecycleError for invalid move', () => {
  const rec = makeRecording();
  assert.throws(() => repo.transitionStatus(rec.id, userId, 'ready'), LifecycleError);
});

test('transitionStatus throws not_found for another user recording', () => {
  const rec = makeRecording();
  const other = db.createUser({ email: 'x@example.com', passwordHash: 'z'.repeat(60) });
  assert.throws(
    () => repo.transitionStatus(rec.id, other.id, 'transcribing'),
    (err) => err.code === 'not_found'
  );
});

test('deleteRecording removes the row and ignores a different user', () => {
  const rec = makeRecording();
  const other = db.createUser({ email: 'del@example.com', passwordHash: 'z'.repeat(60) });
  repo.deleteRecording(rec.id, other.id); // should not delete
  assert.ok(repo.findRecordingById(rec.id, userId), 'row still exists after wrong-user delete');
  repo.deleteRecording(rec.id, userId);
  assert.strictEqual(repo.findRecordingById(rec.id, userId), null, 'row deleted');
});

// ---- repository: transcripts ------------------------------------------------

test('createTranscript and findTranscript enforce ownership via recordings', () => {
  const rec = makeRecording();
  const t = repo.createTranscript({
    recordingId: rec.id,
    provider: 'openai-whisper',
    language: 'en',
    fullText: 'Hello world',
    segments: [{ speaker: 'A', start_ms: 0, end_ms: 1000, text: 'Hello world' }],
  });
  assert.ok(t.id);
  assert.strictEqual(t.recording_id, rec.id);
  assert.strictEqual(t.provider, 'openai-whisper');

  // Own user can retrieve it
  const found = repo.findTranscript(rec.id, userId);
  assert.ok(found, 'owner can read transcript');
  const segments = JSON.parse(found.segments);
  assert.strictEqual(segments[0].speaker, 'A');

  // Another user cannot
  const other = db.createUser({ email: 'tr@example.com', passwordHash: 'w'.repeat(60) });
  assert.strictEqual(repo.findTranscript(rec.id, other.id), null, 'transcript hidden from other user');
});

// ---- repository: summaries --------------------------------------------------

test('createSummary and findSummary enforce ownership via recordings', () => {
  const rec = makeRecording();
  const s = repo.createSummary({
    recordingId: rec.id,
    summaryText: 'Discussed Q3 roadmap.',
    decisions: ['Ship feature X'],
    actionItems: ['@alice: draft spec by Friday'],
  });
  assert.ok(s.id);
  assert.strictEqual(s.recording_id, rec.id);

  const found = repo.findSummary(rec.id, userId);
  assert.ok(found);
  assert.deepStrictEqual(JSON.parse(found.decisions), ['Ship feature X']);
  assert.deepStrictEqual(JSON.parse(found.action_items), ['@alice: draft spec by Friday']);

  const other = db.createUser({ email: 'sum@example.com', passwordHash: 'w'.repeat(60) });
  assert.strictEqual(repo.findSummary(rec.id, other.id), null, 'summary hidden from other user');
});

test('createSummary stores nulls when optional fields omitted', () => {
  const rec = makeRecording();
  const s = repo.createSummary({ recordingId: rec.id });
  assert.strictEqual(s.summary_text, null);
  assert.strictEqual(s.decisions, null);
  assert.strictEqual(s.action_items, null);
});
