# Contributing to Dockd

Thanks for your interest in contributing to Dockd! Here's how to get started.

## Getting Started

1. Fork the repository
2. Clone your fork: `git clone https://github.com/YOUR-USERNAME/dockd.git`
3. Create a feature branch: `git checkout -b feature/your-feature-name`
4. Make your changes
5. Run tests: `python -m pytest`
6. Commit with a clear message: `git commit -m "feat: add scale agent v2 /whoami endpoint"`
7. Push to your fork: `git push origin feature/your-feature-name`
8. Open a Pull Request against `main`

## Commit Message Format

Use [Conventional Commits](https://www.conventionalcommits.org/) with lowercase, present tense:

- `feat:` new feature or capability
- `fix:` bug fix
- `security:` security fix
- `refactor:` code change that doesn't fix a bug or add a feature
- `test:` adding or updating tests
- `docs:` documentation only
- `chore:` build, CI, deps, formatting
- `perf:` performance improvement

Examples:

- `feat: add SentryBackend implementation`
- `fix: scale weight reader losing focus on station 2`
- `security: redact wms_t_ tokens from request logs`
- `docs: clarify forced-password-change flow in README`

## Code Style

- **Python:** PEP 8 with `flake8` enforcement (`max-line-length = 120`, see `.flake8`). Type hints where they aid clarity.
- **HTML / JavaScript (templates):** Two-space indent, single quotes for JS strings, prefer plain DOM over framework dependencies.

## Pull Request Requirements

- All existing tests must pass (`python -m pytest`).
- New features should include tests.
- Update relevant documentation (README, CHANGELOG).
- One feature per PR -- keep them focused.

## Reporting Issues

Open an issue on GitHub with:

- What you expected to happen
- What actually happened
- Steps to reproduce
- Environment info (Python version, OS, ShipRush account variant if relevant)

## License

By contributing, you agree that your contributions will be licensed under the Apache License, Version 2.0.
