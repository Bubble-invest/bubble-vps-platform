# Contributing to Bubble Invest

Thanks for your interest in contributing! This document covers all Bubble Invest open source repos.

## Getting Started

1. Fork the repo you want to contribute to
2. Clone your fork
3. Create a branch: `git checkout -b feat/your-feature`
4. Make your changes
5. Run the tests (each repo has its own test suite)
6. Push and open a PR

## Repos

| Repo | Tests | Language |
|---|---|---|
| [bubble-ops-loop](https://github.com/Bubble-invest/bubble-ops-loop) | `python3 -m pytest` | Python, Bash |
| [bubble-vps-platform](https://github.com/Bubble-invest/bubble-vps-platform) | `.venv/bin/python -m pytest lib/` | Python (pyinfra) |
| [bubble-cabinet](https://github.com/Bubble-invest/bubble-cabinet) | Docker build + shellcheck | Docker, Bash |

## Code Standards

- **TDD**. Every feature starts with a failing test.
- **Python**: type hints (`from __future__ import annotations`), pytest, no uncovered paths.
- **Bash**: `set -euo pipefail`, shellcheck clean, idempotent scripts.
- **Docs**: French and English for user-facing docs. Markdown.

## Pull Requests

- Keep PRs small and focused (one feature/fix per PR)
- Reference the issue number if applicable
- Include test evidence (screenshot or test output) in the PR body
- All PRs require maintainer approval — we review within 24 hours

## Security

If you find a security vulnerability, please **do not** open a public issue.
Email {{OPERATOR_EMAIL}} instead.

## License

MIT. By contributing, you agree that your contributions will be licensed under MIT.
