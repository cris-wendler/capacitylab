# Security

## Reporting a vulnerability

Please do not open a public issue for a security problem.

Report it privately through GitHub: on the repository's **Security** tab, choose **Report a vulnerability**. That
opens a private advisory only the maintainer can see. Include what you found, how to reproduce it, and what an
attacker could do with it.

You can expect an acknowledgement within a week, and an honest answer about whether it will be fixed and when. This is
a portfolio project maintained by one person, so please size your expectations accordingly.

## What is in scope

CapacityLab runs locally and is meant to be bound to localhost. In scope:

- the web UI: session handling, the login path, anything that lets one request act as another;
- reading evidence files, slow logs or exports: a crafted input that executes code or escapes the runs directory;
- the cloud import paths reaching an account they were not asked to reach, or writing anything to one;
- an identifier, credential or customer value being written into evidence, a run ledger or the history store.

Out of scope:

- serving the UI on a public address with no password set. The tool refuses to start that way, and overriding the
  refusal is your decision;
- the synthetic demo data, which is fictional by design;
- spend: the budget caps are a guard against mistakes, not a security boundary.

## Handling credentials

The repository holds no credentials. Keys live in `.env`, which is git-ignored, or in the environment. Secrets are
checked for presence only and never logged, rendered or written into a run ledger. Cloud imports make read-only calls
and store no account identifier: instances become `writer` and `reader-N`, and a cluster is keyed by a salted HMAC
that stays on your machine. If you find a case where any of that is not true, it is a vulnerability and this page is
how to report it.
