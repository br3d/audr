import js from '@eslint/js'
import tseslint from 'typescript-eslint'
import eslintConfigPrettier from 'eslint-config-prettier'
import globals from 'globals'

export default tseslint.config(
  { ignores: ['dist', 'coverage', 'playwright-report', 'test-results'] },
  js.configs.recommended,
  // Not recommendedTypeChecked: typescript-eslint has no release that loads
  // against TypeScript 7 (this project's tsc) — it throws at startup rather
  // than lint. See https://github.com/typescript-eslint/typescript-eslint/issues/10940.
  // Revisit once that ships; `npm run typecheck` (tsc --noEmit) is the
  // type-aware gate in the meantime.
  ...tseslint.configs.recommended,
  {
    languageOptions: {
      globals: { ...globals.browser, ...globals.node },
    },
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
    },
  },
  eslintConfigPrettier,
)
