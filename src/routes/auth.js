'use strict';

const express = require('express');
const service = require('../auth/service');
const { requireAuth, requireGuest } = require('../auth/middleware');
const { csrfSynchronisedProtection } = require('../auth/csrf');
const { loginLimiter, resetRequestLimiter } = require('../auth/rate-limit');

/**
 * All auth-related HTTP routes: sign-up, sign-in, sign-out, password reset, and
 * account management. Server-rendered with EJS. Every state-changing route is
 * behind CSRF protection (applied at the app level for unsafe methods).
 */
const router = express.Router();

/** Only allow same-origin relative redirects for the post-login `next` param. */
function safeNext(next) {
  if (typeof next === 'string' && next.startsWith('/') && !next.startsWith('//')) {
    return next;
  }
  return '/account';
}

/**
 * Log a user in by (re)creating a fresh session bound to their id. Regenerating
 * the session id on privilege change defeats session-fixation attacks.
 */
function establishSession(req, user) {
  return new Promise((resolve, reject) => {
    req.session.regenerate((err) => {
      if (err) return reject(err);
      req.session.userId = user.id;
      req.session.save((saveErr) => (saveErr ? reject(saveErr) : resolve()));
    });
  });
}

function renderError(res, view, status, extra) {
  return res.status(status).render(view, { error: null, values: {}, ...extra });
}

// ---- Sign up ---------------------------------------------------------------

router.get('/signup', requireGuest, (req, res) => {
  res.render('signup', { error: null, values: {} });
});

router.post('/signup', requireGuest, async (req, res, next) => {
  const { email, password } = req.body;
  try {
    const user = await service.signUp(email, password);
    await establishSession(req, user);
    res.redirect('/account');
  } catch (err) {
    if (err instanceof service.AuthError) {
      return renderError(res, 'signup', 400, { error: err.message, values: { email } });
    }
    next(err);
  }
});

// ---- Sign in ---------------------------------------------------------------

router.get('/login', requireGuest, (req, res) => {
  res.render('login', { error: null, values: {}, next: req.query.next || '' });
});

router.post('/login', requireGuest, loginLimiter, async (req, res, next) => {
  const { email, password } = req.body;
  try {
    const user = await service.authenticate(email, password);
    await establishSession(req, user);
    res.redirect(safeNext(req.body.next));
  } catch (err) {
    if (err instanceof service.AuthError) {
      return renderError(res, 'login', 401, {
        error: err.message,
        values: { email },
        next: req.body.next || '',
      });
    }
    next(err);
  }
});

// ---- Sign out --------------------------------------------------------------

router.post('/logout', (req, res, next) => {
  req.session.destroy((err) => {
    if (err) return next(err);
    res.clearCookie('sid');
    res.redirect('/login');
  });
});

// ---- Forgot / reset password ----------------------------------------------

router.get('/forgot-password', (req, res) => {
  res.render('forgot-password', { error: null, sent: false, values: {} });
});

router.post('/forgot-password', resetRequestLimiter, async (req, res, next) => {
  const { email } = req.body;
  try {
    await service.requestPasswordReset(email);
  } catch (err) {
    return next(err);
  }
  // Always show the same confirmation, regardless of whether the email exists.
  res.render('forgot-password', {
    error: null,
    sent: true,
    values: { email },
  });
});

router.get('/reset-password', (req, res) => {
  const token = req.query.token || '';
  res.render('reset-password', { error: null, token, values: {} });
});

router.post('/reset-password', async (req, res, next) => {
  const { token, password } = req.body;
  try {
    await service.resetPassword(token, password);
    res.render('message', {
      title: 'Password updated',
      heading: 'Password updated',
      body: 'Your password has been changed. You can now sign in with your new password.',
      link: { href: '/login', label: 'Go to sign in' },
    });
  } catch (err) {
    if (err instanceof service.AuthError) {
      return res.status(400).render('reset-password', {
        error: err.message,
        token,
        values: {},
      });
    }
    next(err);
  }
});

// ---- Account settings (protected) -----------------------------------------

router.get('/account', requireAuth, (req, res) => {
  res.render('account', { user: req.user, notice: req.query.notice || null, error: null });
});

router.post('/account/email', requireAuth, async (req, res, next) => {
  try {
    await service.changeEmail(req.user.id, req.body.email);
    res.redirect('/account?notice=email-updated');
  } catch (err) {
    if (err instanceof service.AuthError) {
      return res.status(400).render('account', { user: req.user, notice: null, error: err.message });
    }
    next(err);
  }
});

router.post('/account/password', requireAuth, async (req, res, next) => {
  const { currentPassword, newPassword } = req.body;
  try {
    await service.changePassword(req.user.id, currentPassword, newPassword);
    res.redirect('/account?notice=password-updated');
  } catch (err) {
    if (err instanceof service.AuthError) {
      return res.status(400).render('account', { user: req.user, notice: null, error: err.message });
    }
    next(err);
  }
});

router.post('/account/delete', requireAuth, (req, res, next) => {
  const userId = req.user.id;
  req.session.destroy((err) => {
    if (err) return next(err);
    try {
      service.deleteAccount(userId);
    } catch (e) {
      return next(e);
    }
    res.clearCookie('sid');
    res.render('message', {
      title: 'Account deleted',
      heading: 'Your account has been deleted',
      body: 'We are sorry to see you go. Your account and data have been removed.',
      link: { href: '/signup', label: 'Create a new account' },
    });
  });
});

// The CSRF guard is attached by the app for unsafe methods before this router,
// but export it here too so the router is self-contained if mounted elsewhere.
router.csrfProtection = csrfSynchronisedProtection;

module.exports = router;
