# Security policy

## Reporting a vulnerability

Please report security issues privately, not in a public issue.

Use GitHub's private reporting: open the repository's **Security** tab and choose **Report a vulnerability**. If that option is not shown, open a public issue that says only that you have a security concern, without details, and I will arrange a private channel.

This is a personal project, so there is no guaranteed response time. I aim to acknowledge reports within a week.

## Supported versions

Only the latest commit on `main` is supported.

## Scope notes

- API keys and tokens belong in a local `.env` file, which is git-ignored (see `.env.example` for the variable names). If you find a real key anywhere in this repository or its history, please report it privately.
