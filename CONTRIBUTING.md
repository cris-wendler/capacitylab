# Contributing

Thank you for considering a contribution. Follow the four steps below, in order.

1. **Check it is not already there.** Read the [README](README.md), run `capacitylab --help` and
   `capacitylab <command> --help`, and check [CHANGELOG.md](CHANGELOG.md). It may already exist under another name.

2. **Search the issues**, open and closed. If one matches, add your details there rather than opening a second one for
   the same problem.

3. **Open an issue.** A bug report for something that does not work as documented, with the command, the output, and
   your Python version. A proposal for something new, describing the problem before the change. The reply tells you
   whether it fits before you spend time on it.

4. **Open a pull request** from a fork, on its own branch, referencing the issue, for example `Fixes #12`.

Small fixes such as a typo or a broken link can go straight to step 4.

> [!IMPORTANT]
> Never open a public issue for a security problem. Follow [SECURITY.md](SECURITY.md), which explains how to report it
> privately.

## What fits

CapacityLab models a capacity decision and has people argue about it. The numbers come from deterministic code and the
judgment comes from language models, and that split is the point. Changes that fit:

- more evidence a decision can rest on (a new importer, a new lab measurement, a new cloud read path);
- more ways to be honest about uncertainty (gaps, caveats, contradictions between sources);
- options that compete on cost and risk, priced from evidence;
- work on the model itself: queueing, cost arithmetic, attribution.

Changes that do not fit:

- a number an agent asserts that no code can check;
- anything that sends real customer data anywhere, or that stores an identifier from someone's account;
- an agent framework. The orchestration is plain Python on purpose, and that is a design decision, not an omission.

## Setting up

```bash
python3 -m venv .venv                   # Python 3.11 or newer
.venv/bin/pip install -e ".[dev,all]"   # "[dev]" alone is enough for most of the suite
make check                              # lint, tests, a demo run, replay, and the identifier scan
```

`make check` is what CI runs on every change. The suites that need containers (MySQL, PostgreSQL, and the AWS, Google
Cloud and Azure emulators) run with `make check-containers`, or in CI on request.

## House rules

- **Every money figure follows from its inputs by arithmetic.** `tests/test_money_adds_up.py` enforces it. Compare a
  one-off against a monthly cost only on a twelve-month basis.
- **Label what a number is.** Observed, measured, modeled, forecast or assumption. An unsupported number is flagged in
  the record, never silently accepted.
- **No real identifiers.** `capacitylab scan` must report zero findings. Fake names in tests are split across string
  literals so the scan does not match the test file itself.
- **Tests before the change is done.** New behaviour comes with a test that would fail without it.
- **No cloud calls in tests.** Cloud paths are tested against recorded responses or a local emulator, never a real
  account.
- Keep to the style already in the file: `ruff check src tests scripts` passes, lines stay within 127 characters, and
  comments explain why rather than what.

## Running an LLM review

The default provider is `mock`, which is scripted, offline and free. A real model costs money, so it is opt-in through
`CAPACITYLAB_PROVIDER` and capped by `CAPACITYLAB_MAX_USD_PER_RUN` and `CAPACITYLAB_MAX_USD_TOTAL`. Please do not add
tests that call a paid API; the `live` marker exists for the few that do and is excluded by default.
