import babelParser from '@babel/eslint-parser'
import { defineConfig, globalIgnores } from 'eslint/config'

const parserOptions = plugins => ({
  requireConfigFile: false,
  babelOptions: { babelrc: false, configFile: false, parserOpts: { plugins } },
})

export default defineConfig(
  globalIgnores(['.runtime/**', 'dist/**', 'test-results/**', 'playwright-report/**']),
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: { parser: babelParser, parserOptions: parserOptions(['typescript']) },
    rules: { 'no-debugger': 'error', 'no-unreachable': 'error' },
  },
  {
    files: ['**/*.tsx'],
    languageOptions: { parserOptions: parserOptions(['typescript', 'jsx']) },
  },
)
