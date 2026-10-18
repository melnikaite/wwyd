# Security policy

## Supported versions

wwyd is pre-release. Only the latest `main` (and the latest pre-release, once published)
receives fixes.

## Reporting a vulnerability

Report privately through GitHub private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Please do not open public issues,
discussions or pull requests for vulnerabilities.

Include steps to reproduce with synthetic data only.

## Scope

In scope, besides the usual security bugs:

- Privacy leaks: raw input or contact identities reaching the database, logs, error
  reports or exports.
- Visibility bypass: a third party seeing claims the owner did not allow.
- Auth or scope bypass: a client calling tools or reading personas beyond its grant.

## Response

This is a volunteer project; handling is best effort. We aim to acknowledge reports within
7 days and will keep you informed about the fix and disclosure.
