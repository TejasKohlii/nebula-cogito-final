ShinkaEvolve runtime vendored for Cogito

Upstream: https://github.com/SakanaAI/ShinkaEvolve
Revision: 9912af12d423504b8d580f4179fd15f5f88b8c50
Version: 0.0.7; license: Apache-2.0 (see LICENSE).

Cogito retains local changes to shinka/core/async_runner.py (seed-only
resume and persisted-generation counting) and shinka/llm/providers/headless.py
(process-group timeout/cancellation cleanup and bounded pipe handling).
Direct Anthropic support also retains Sonnet 5 request parameters and fallback
pricing, plus immediate failure for non-retryable API errors.
The Python package, package assets and install metadata are included.
Upstream Git history, docs, examples, tests and development tooling are omitted.
Do not replace this directory with an unpinned pip installation.
