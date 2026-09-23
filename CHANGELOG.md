# Changelog

Notable changes, newest first.

## 0.1.2, 2026-09-23

**Changed**

- The MySQL, PostgreSQL and cloud emulator suites now run on every push, not only when asked for. They run the
  commands from the docs as written, so a command that stops working fails the build.
- One set of words everywhere. The tool used to say "stakeholder simulation" and "MOCK provider" where the docs said
  "five-agent review" and "scripted agents". It now says the same as the docs.

**Fixed**

- Docs left broken when the README was split up: a lab file no command creates, a missing install step, cloud imports
  shown before the emulator that serves them, a role named "cost analyst" that does not exist, an Ollama model name
  that did not match, a setting that only applies to the web UI, and two baselines with three different names.
- The same result was told twice in two places. It is now in one place, with links to it.

## 0.1.1, 2026-09-23

**Fixed**

- The query rewrite check took about 20 seconds on SQLite 3.53 (for example Homebrew's Python 3.14), and measured
  one candidate's work 1,500 times higher than on older SQLite. It now takes under a second on both, and the
  measurements agree within 3%.
- `capacitylab replay` now names both engine versions when a run was recorded on a different one, instead of only
  reporting that the run did not verify. Work measurements differ between engine versions, so this is expected
  rather than a sign that the record was changed. The README says so too.

## 0.1.0, 2026-09-22

First tagged release. Everything below is new.

**Review**

- Five-agent review of a database capacity decision (database engineer, application owner, reliability engineer,
  FinOps analyst, tenant representative), with a decision record that keeps disagreements, unanswered challenges and
  evidence nobody has.
- Scripted agents that run offline with no API key and give the same result every time. This is the default.
- Language model agents through the Anthropic API or any OpenAI-compatible endpoint, with token counting before each
  call and hard spend limits per run and in total.
- Every agent turn is checked: citations must exist and be visible to that agent, numbers must appear in the cited
  evidence, requested checks must be allowed for that role.
- Replay of any run record, re-running every check and confirming the same results.
- `capacitylab evaluate`: the five-agent review against a single agent and a simple threshold rule, scored against
  assumptions none of them saw.

**Models**

- M/M/c CPU queueing model per 15-minute slot, and options priced from the scenario's rate card.
- Load attribution: each slot over the threshold broken down by statement, tenant and batch job.
- Tenant shares compared with what each tenant's plan guarantees.
- Query rewrites as options, credited only after an equivalence check has measured them.

**Evidence**

- Two sample scenarios, `campaign-overlap` and `downsize-reader`, with synthetic topology, metrics, digests, plans,
  SLOs, rate card and budget.
- SQLite experiment database for index and rewrite experiments; MySQL 8.0 as an option.
- Local lab on MySQL 8.0 and PostgreSQL 17 in Docker, with Percona Toolkit support.
- File importers for slow logs, digest exports, `EXPLAIN ANALYZE`, CloudWatch exports and Percona Toolkit reports.
- Read-only imports from AWS, Google Cloud and Azure, against local emulators unless `--live` is passed.
- History store and envelope: `capacitylab history collect|status|envelope`.

**Interface**

- Command line for every feature, and a local web UI (`capacitylab serve`) with optional password sign-in and a model
  settings page.

**Project**

- Licensed under AGPL-3.0-or-later, with a commercial license available separately.
- Direct dependencies pinned to exact versions.
- CI on every push: lint, the test suite on Python 3.11 to 3.14, every command named in the README, and the README
  quickstart run exactly as written. Container suites run on request.
