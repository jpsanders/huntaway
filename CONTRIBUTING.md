# Contributing

Issues and pull requests are welcome. Bug reports with `huntaway doctor` output and the events around the problem
(`huntaway events --json`) are the easiest to act on.

## Set up

```sh
git clone https://github.com/jpsanders/huntaway && cd huntaway
python3.15 -m venv .venv && . .venv/bin/activate
make init-dev
```

## Before a pull request

```sh
make format      # black and isort
make checks      # every check CI runs, in the same order
```

`make checks` runs isort, black, editorconfig-checker, darglint, flake8, mypy in strict mode, bandit, the man page
check and pytest with coverage. CI runs the same on Linux and macOS with Python 3.14 and 3.15.

## Changing things

- **The dashboard** lives in `src/huntaway/web`. The stylesheet is built from `web-src/input.css` with the standalone
  Tailwind CLI: `make css` downloads it and rebuilds `app.css`. Commit the built file, so installs need no Node.
- **The manual** is `docs/MANUAL.md`. Its options section is generated from the settings schema in `config.py`.
  `make man` rebuilds the manual's options, the man page and the packaged copy.
- **A new setting** is one `Option` in `config.SCHEMA`. The dashboard, the config file, `huntaway config` and the
  manual pick it up from there.
- **A new harness** is one `Harness` in `agents/harnesses.py`, plus a discoverer when its sessions live in files.
- **Tests never act on real processes** except children they start themselves. Keep it that way.
