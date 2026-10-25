# Contributing to Breakwise

Thank you for helping. Breakwise is a registry of breaking changes in Python libraries, so the most useful contribution is a notice: one breaking change, with an example that runs on real releases.

Everyone who takes part follows the [code of conduct](CODE_OF_CONDUCT.md). To report a security problem, see the [security policy](SECURITY.md).

## Ask for a notice

Did an upgrade break your code? [Request a notice](https://github.com/breakwise/breakwise/issues/new?template=request-notice.yml) with the package, the release that broke your code, and a link to an official source: the package's changelog, release notes, docs or source code.

## Write a notice

1. Read [the notice format](../docs/notice-format.md). It describes every field and rule.
2. Take the next free ID, and add the notice as `notices/pypi/<package>/<id>.yaml`, with `status: draft`.
3. Link only official sources of the package.
4. Keep the examples as small as possible. They run offline, with fake API keys.
5. Check the notice locally, then open a pull request. Its template has a checklist.

A maintainer checks every notice against its sources and then sets `verified`.

## Check your work locally

You need Python 3.12 or later. The verifier also needs Linux or macOS and network access.

```sh
python -m venv .venv
source .venv/bin/activate
python -m pip install --require-hashes -r requirements-dev.txt
python tools/validate_notices.py
python tools/verify_examples.py --python 3.12 notices/pypi/<package>/<id>.yaml
```

On Windows, activate the environment with `.venv\Scripts\activate` instead.
In PowerShell, activating needs scripts to be allowed. If they aren't, use `.venv\Scripts\python` in place of `python` in the commands above.
`--python 3.12` runs the examples on Python 3.12, because old releases of a package may not install on a newer Python.
uv finds Python 3.12, or installs it if it is missing.

The verifier runs the examples of a notice on your machine. Run it only on notices whose examples you have read: an example can read your environment variables, such as tokens, and reach the network.

The [Development section of the README](../README.md#development) lists every check that the pull request runs.

## What the pull request checks do

- **Validate notices** lints the code, the docs and the workflows, runs the tests with full coverage, validates every notice and builds the package.
- **Test tools (Python 3.13)** and **Test tools (Python 3.14)** run the tests on newer Python.
- **Test on Windows** and **Test on macOS** run the tests and validate the notices on those systems, with their own settings. Then they install the package and check that what it writes to files, a notice and an error message, is UTF-8.
- **Verify examples** installs real releases from PyPI and runs the examples of the notices that the pull request changes, or every notice when the tools change.

For contributors from outside the project, a maintainer approves each run before it starts.

## Change the code

- Every change in behavior comes with a test, and the tests cover every line and branch of `src/` and `tools/`.
- The code passes ruff and mypy in strict mode.
- Commit messages are in the imperative, such as "Add the notice for ...".

## Releases

Maintainers release `breakwise` to PyPI from a tag:

1. Raise `__version__` in `src/breakwise/__init__.py` in a pull request.
2. Once it is merged, tag that commit of `main` with an annotated tag, and push the tag:

   ```sh
   git tag -a v0.3.0 -m "Breakwise 0.3.0" <commit>
   git push origin v0.3.0
   ```

3. The release workflow checks that the tag is on `main` and matches the version. Then it runs every check of a pull request on the tagged commit, with the examples of every notice on real releases, the latest one included. It builds the package, runs it, and waits for a maintainer to approve it in the `pypi` environment. Then it publishes with Trusted Publishing, with attestations.
4. If a step fails, nothing is published. Re-run the failed jobs when the cause was temporary, such as PyPI being unreachable. Otherwise, such as when a new release of a package fails a notice, fix the cause on `main` and release the next patch version. A tag is never moved or reused.

## Licenses

A contribution is licensed like the part of the repository it changes.
Contributions to the code fall under section 5 of the [Apache License 2.0](../LICENSE), which the code is licensed under.
Contributions to the notices are licensed under [CC BY 4.0](../notices/LICENSE.md), like the notices themselves.
