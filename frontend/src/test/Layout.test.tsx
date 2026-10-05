import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import React from 'react'
import { act } from 'react'
import { createRoot } from 'react-dom/client'
import type { Root } from 'react-dom/client'
import Layout, { type MainPage } from '../components/Layout'
import pkg from '../../package.json'

describe('Layout', () => {
  let container: HTMLDivElement
  let root: Root

  beforeEach(() => {
    container = document.createElement('div')
    document.body.appendChild(container)
    root = createRoot(container)
  })

  afterEach(async () => {
    await act(async () => {
      root.unmount()
    })
    document.body.removeChild(container)
    vi.clearAllMocks()
  })

  function mount(onSignOut: () => void, setPage: (page: MainPage) => void) {
    act(() => {
      root.render(
        React.createElement(Layout, {
          page: 'dashboard',
          setPage,
          onSignOut,
          children: React.createElement('div', { 'data-testid': 'page-body' }, 'content'),
        }),
      )
    })
  }

  function avatarButton() {
    return container.querySelector('[aria-label="Account menu"]') as HTMLButtonElement
  }

  function menuItem(label: string) {
    return Array.from(container.querySelectorAll('[role="menuitem"]')).find(
      (b) => b.textContent === label,
    ) as HTMLButtonElement | undefined
  }

  it('does not render a Holdings nav entry (AUD-405 retired the standalone page)', () => {
    mount(vi.fn(), vi.fn())
    const navLabels = Array.from(container.querySelectorAll('.sidebar-nav .nav-item')).map(
      (b) => b.textContent,
    )
    expect(navLabels).not.toContain('Holdings')
  })

  it('does not render an Assistant nav entry (AUD-436 pulled the prototype out of the menu)', () => {
    mount(vi.fn(), vi.fn())
    const navText = container.querySelector('.sidebar-nav')?.textContent ?? ''
    expect(navText).not.toContain('Assistant')
  })

  it('titles the prototype route so it cannot be mistaken for a feature (AUD-436)', () => {
    act(() => {
      root.render(
        React.createElement(Layout, {
          page: 'assistant-prototype',
          setPage: vi.fn(),
          onSignOut: vi.fn(),
          children: React.createElement('div', null, 'content'),
        }),
      )
    })
    expect(container.querySelector('.topbar-title')?.textContent).toBe('Assistant (prototype)')
  })

  it('renders the space card, nav section label, and self-hosted status without a Sign out button in the sidebar', () => {
    mount(vi.fn(), vi.fn())
    expect(container.querySelector('.space-card')?.textContent).toContain('Personal portfolio')
    expect(container.querySelector('.space-card')?.textContent).toContain('Local space')
    expect(container.querySelector('.nav-section-label')?.textContent).toBe('Navigation')
    expect(container.querySelector('.sidebar-footer')?.textContent).toContain(
      'Self-hosted · your data stays yours',
    )
    expect(container.querySelector('.sidebar-footer')?.textContent).not.toContain('Sign out')
  })

  it('renders a user icon on the account menu button instead of a text initial (AUD-416)', () => {
    mount(vi.fn(), vi.fn())
    const btn = avatarButton()
    expect(btn.querySelector('.avatar-button-icon')).toBeTruthy()
    expect(btn.textContent).toBe('')
  })

  it('keeps the menu closed until the avatar button is clicked', () => {
    mount(vi.fn(), vi.fn())
    expect(avatarButton().getAttribute('aria-expanded')).toBe('false')
    expect(container.querySelector('[role="menu"]')).toBeNull()
  })

  it('opens the menu on avatar click and shows exactly one Sign out control in the whole layout', async () => {
    mount(vi.fn(), vi.fn())
    await act(async () => {
      avatarButton().click()
    })
    expect(avatarButton().getAttribute('aria-expanded')).toBe('true')
    expect(container.querySelector('[role="menu"]')).toBeTruthy()
    const signOutButtons = Array.from(container.querySelectorAll('button')).filter(
      (b) => b.textContent === 'Sign out',
    )
    expect(signOutButtons.length).toBe(1)
  })

  it('calls onSignOut and closes the menu when Sign out is clicked', async () => {
    const onSignOut = vi.fn()
    mount(onSignOut, vi.fn())
    await act(async () => {
      avatarButton().click()
    })
    await act(async () => {
      menuItem('Sign out')!.click()
    })
    expect(onSignOut).toHaveBeenCalledOnce()
    expect(container.querySelector('[role="menu"]')).toBeNull()
  })

  it('calls setPage("account") and closes the menu when Account & Data is clicked', async () => {
    const setPage = vi.fn()
    mount(vi.fn(), setPage)
    await act(async () => {
      avatarButton().click()
    })
    await act(async () => {
      menuItem('Account & Data')!.click()
    })
    expect(setPage).toHaveBeenCalledWith('account')
    expect(container.querySelector('[role="menu"]')).toBeNull()
  })

  it('closes the menu on Escape', async () => {
    mount(vi.fn(), vi.fn())
    await act(async () => {
      avatarButton().click()
    })
    expect(container.querySelector('[role="menu"]')).toBeTruthy()
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    })
    expect(container.querySelector('[role="menu"]')).toBeNull()
  })

  it('closes the menu on an outside click', async () => {
    mount(vi.fn(), vi.fn())
    await act(async () => {
      avatarButton().click()
    })
    expect(container.querySelector('[role="menu"]')).toBeTruthy()
    await act(async () => {
      document.body.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }))
    })
    expect(container.querySelector('[role="menu"]')).toBeNull()
  })

  // AUD-407: the sidebar version is a compile-time constant from package.json,
  // not an API call, so it must render with no network and no session.
  it('shows the build version in the sidebar footer', () => {
    mount(vi.fn(), vi.fn())
    const label = container.querySelector('.sidebar-footer [data-testid="app-version"]')
    expect(label).toBeTruthy()
    expect(label!.textContent).toBe(`v${pkg.version}`)
    expect(label!.getAttribute('title')).toContain(`audr v${pkg.version}`)
  })
})
