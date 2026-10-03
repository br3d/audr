# Brand assets

The audr logo is a mountain silhouette whose peaks are drawn as candlestick
charts, with the `audr` wordmark set beneath it. It is single-colour line art:
there is one shape, and the only thing that changes between contexts is the ink
colour — white on dark surfaces, near-black on light ones.

## Where the files live

| Path | What it is |
| --- | --- |
| `assets/brand/audr-logo-{black,white}-{1024,2048}.jpg` | The original artwork as supplied. `black` is the black lockup on white paper, `white` is the white lockup on black. Treat these as the masters — do not edit them in place. |
| `assets/brand/audr-logo-{light,dark}-{512,1024}.png` | The full lockup (mark + wordmark) on a transparent background. `light` is inked near-black for light backgrounds; `dark` is inked white for dark backgrounds. |
| `assets/brand/audr-mark-{light,dark}-{64,180,256,512}.png` | The mark on its own, without the wordmark, for favicons and tight spaces. |
| `frontend/public/brand/` | The subset the SPA actually loads, served from the site root at `/brand/…`: both lockups, both marks, `favicon.ico` (16/32/48/64) and `apple-touch-icon.png`. |

The JPEG masters are never used directly in the product. JPEG has no alpha
channel, so the baked-in white or black rectangle is visible on any surface that
is not exactly that colour.

## Regenerating the derived assets

Every PNG and the `.ico` are generated from the single 2048 px black-on-white
master by `scripts/gen_brand_assets.py`, which keys the paper out to
transparency, re-inks the artwork for each theme, and splits the mark off the
lockup by locating the blank band between them. The outputs are committed, so
you only need to re-run it when the artwork itself changes:

```bash
python3 -m pip install --user pillow   # tooling-only dependency
python3 scripts/gen_brand_assets.py
```

## Using the logo in the SPA

Use the `BrandMark` and `BrandLockup` components from
`frontend/src/components/Logo.tsx`. They render a span styled by `.brand-mark` /
`.brand-lockup` in `frontend/src/styles/folio.css`, and the light/dark ink is
swapped there off the `data-theme` attribute on the document root. Doing the
swap in CSS rather than in React matters because the sign-in and setup screens
render outside the theme provider but still inherit the persisted theme.

Pass `label={null}` when an adjacent text label already names the brand — the
sidebar header and the auth cards both put the word "audr" next to the mark, so
an accessible name on the image itself would be read out twice.

## Usage notes

- Keep clear space around the lockup of at least the height of the `a` in the
  wordmark.
- Do not recolour the artwork to anything other than the two inks above, add
  effects to it, or set it on a mid-tone background where neither ink has
  contrast.
- Prefer the mark alone below roughly 48 px — the wordmark stops being legible.
