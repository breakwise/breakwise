# Security policy

## Report a vulnerability

Please don't report a security problem in a public issue. Report it privately instead:

- **On GitHub**, which is the preferred way: use [private vulnerability reporting](https://github.com/breakwise/breakwise/security/advisories/new). Only the maintainers can see the report.
- **Without a GitHub account:** email [breakwise-org@outlook.com](mailto:breakwise-org@outlook.com).

Say what is affected, how to reproduce the problem, and what an attacker could do with it.
We confirm that we received your report within 7 days, and we keep you informed until it is fixed.

## Scope

- The `breakwise` package on PyPI and its command line.
- The tools in `tools/` and the workflows in `.github/workflows/`, including the release to PyPI.
- The notices, for example a notice whose fix would make code unsafe.

CI runs the examples in notices by design. The job has read-only permissions and no secrets, and pull requests from outside contributors need a maintainer's approval before they run. The verifier also blocks network calls and stops examples that run too long, but only to prevent accidents: an example can get around both, so doing that is not a vulnerability by itself. A way for a notice or a pull request to get a write token or a secret, to run without that approval, or to change anything outside its own job, such as a cache or a release, is in scope.

## Supported versions

Only the latest release of `breakwise` gets security fixes.
