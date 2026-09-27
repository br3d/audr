'use strict';

/**
 * Recording processing lifecycle.
 *
 * Allowed transitions:
 *   uploaded → transcribing → summarizing → ready
 *   Any non-terminal → failed
 *   failed is terminal (no outbound transitions)
 */

const STATUSES = Object.freeze(['uploaded', 'transcribing', 'summarizing', 'ready', 'failed']);

const TRANSITIONS = Object.freeze({
  uploaded:     Object.freeze(['transcribing', 'failed']),
  transcribing: Object.freeze(['summarizing', 'failed']),
  summarizing:  Object.freeze(['ready', 'failed']),
  ready:        Object.freeze(['failed']),
  failed:       Object.freeze([]),
});

function isValidStatus(s) {
  return typeof s === 'string' && STATUSES.includes(s);
}

function canTransition(from, to) {
  const allowed = TRANSITIONS[from];
  return Boolean(allowed && allowed.includes(to));
}

class LifecycleError extends Error {
  constructor(message) {
    super(message);
    this.name = 'LifecycleError';
  }
}

function assertTransition(from, to) {
  if (!isValidStatus(from)) throw new LifecycleError(`Unknown source status: '${from}'`);
  if (!isValidStatus(to)) throw new LifecycleError(`Unknown target status: '${to}'`);
  if (!canTransition(from, to)) {
    throw new LifecycleError(`Cannot transition from '${from}' to '${to}'`);
  }
}

module.exports = { STATUSES, TRANSITIONS, isValidStatus, canTransition, assertTransition, LifecycleError };
