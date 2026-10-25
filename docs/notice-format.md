# Notice format (v0.3)

A **notice** describes one breaking change in one package.
Each notice is a YAML file stored at:

```text
notices/<ecosystem>/<package>/<id>.yaml
```

- `<ecosystem>` and `<package>` are lowercase, e.g. `notices/pypi/pydantic-ai/`.
- The file name is the notice ID, e.g. `BW-2026-0001.yaml`.

Example: [`notices/pypi/pydantic-ai/BW-2026-0001.yaml`](../notices/pypi/pydantic-ai/BW-2026-0001.yaml)

The machine-readable definition is [`schema/notice-0.3.schema.json`](../schema/notice-0.3.schema.json)
(JSON Schema, draft 2020-12). If this page and the schema disagree, the schema wins.
A released format never changes: [`schema/notice-0.1.schema.json`](../schema/notice-0.1.schema.json)
and [`schema/notice-0.2.schema.json`](../schema/notice-0.2.schema.json) stay as they were,
and the tools read only the latest format.

The format may change before v1.0. Every change is listed in the [changelog](#changelog) below.

## Fields

| Field | Required | Meaning |
| --- | --- | --- |
| `schema_version` | yes | Version of this format: `"0.3"`. |
| `id` | yes | Unique ID `BW-<year>-<number>`: the year it was published and a number of four to eight digits, without extra leading zeros, e.g. `BW-2026-0001` or `BW-2026-10000`. Never reused. |
| `status` | yes | `draft` (not reviewed yet) or `verified` (checked by a maintainer against the official sources). |
| `published` | yes | Date the notice was added, as `"YYYY-MM-DD"`. |
| `modified` | no | Date of the last change to the notice, as `"YYYY-MM-DD"`. |
| `package.ecosystem` | yes | Where the package is published. Currently only `PyPI`. |
| `package.name` | yes | Normalized package name: lowercase, words joined by `-`, e.g. `pydantic-ai`. |
| `package.aliases` | no | Other packages that ship the same code with the same version numbers, e.g. `pydantic-ai-slim`. |
| `type` | yes | `rename`, `removal`, `signature` (parameters changed) or `behavior` (same code, different result). |
| `breaks_in` | yes | First version in which the old code stops working. |
| `works_since` | no | The first release in which the new code works, so the fix can be applied before upgrading. Leave it out if that release can't be found or doesn't install on Python 3.12, for example when the new code already works on the oldest release that installs. Without `works_since`, that release is unknown, so the fix waits until after the upgrade. |
| `restored_in` | no | The first release in which the old code works again, for example when the package brings an old name back. A notice applies to an upgrade only when the target is before it. |
| `summary` | yes | One sentence, 10 to 200 characters. |
| `rename.from`, `rename.to` | when `type` is `rename` | Old and new names. Without `rename.keyword_of`, they are names as users import them: a module, class, function, method or attribute, written as its full path, e.g. `pydantic_ai.models.openai.OpenAIModel`, or `pydantic_ai` for a top-level module. With it, they are the old and new names of a keyword argument, e.g. `mcp_servers` and `toolsets`. Tools rewrite imports, usages and keyword arguments automatically. |
| `rename.keyword_of` | no | For a renamed keyword argument: the callables that take it, as full dotted paths of two parts or more, e.g. `pydantic_ai.Agent` or `pydantic_ai.Agent.run`. |
| `instructions` | for every other `type`, and never with `rename` | Plain steps to migrate, for people and coding agents, at least 10 characters. |
| `affects` | for every other `type`, and never with `rename` | The code that the change breaks. An item is a path, written like `rename.from`, e.g. `pydantic_ai.Agent.run_stream`. Or it is a callable with one of its keyword arguments: `path`, `keyword`, and `when`, which is `passed` (the default) when code breaks by passing the keyword, or `omitted` when it breaks by leaving it out, as when a default changed. `when` needs `keyword`. |
| `example.before`, `example.after` | yes | The smallest code that shows the change. |
| `example.fails_with.before` | for every other `type`; optional for `rename` | How `example.before` fails on `breaks_in`: `error` is the exception, bare for a builtin such as `TypeError` and as its full path otherwise, such as `pydantic_ai.exceptions.UserError`, and the optional `message` is a part of its message. For a `behavior` change, `error` is `AssertionError`. |
| `example.fails_with.after` | for every other `type` that has `works_since`; optional for `rename`; never without `works_since` | How `example.after` fails on the release just before `works_since`, written like `example.fails_with.before`. |
| `references` | yes | `https` links to official sources: changelog, migration guide, release notes or source code. |
| `withdrawn.date`, `withdrawn.reason` | no | For a notice that should never have existed: the date it was withdrawn, as `"YYYY-MM-DD"`, and why, at least 10 characters. Its ID stays taken. |

Versions follow the package's own scheme ([PEP 440](https://peps.python.org/pep-0440/) for PyPI),
with an epoch when the package has one, e.g. `"1!2.0"`, and are always quoted, e.g. `"2.0.0"`,
so YAML keeps them as text: unquoted, `2.10` would become the number 2.1. The same applies to dates.
Every value except the example code is one line: a newline, even at the end, is not allowed.
Notices are plain YAML, without anchors, aliases or tags.

## Rules

1. **One change per notice.** Two renames in the same release are two notices.
2. **Official sources only.** Every notice links to the changelog, docs, release notes or source code
   of the package itself.
3. **Automatic fixes first.** Use `rename` whenever the change is a pure rename,
   with the old name in `example.before` and only the new name in `example.after`.
   A `rename` has no `instructions`: use them only when a person has to decide.
   `rename` covers names that code imports or uses: modules, classes, functions, methods and attributes.
   A path is one that an import and then `getattr` reach from a module,
   so an attribute that only instances get, such as one set in `__init__`, can't be renamed this way.
   `example.before` uses `rename.from` by that exact path, directly or through an import of it,
   and `example.after` uses `rename.to` the same way and doesn't use `rename.from`.
   A method or an attribute that the example reaches through an object, such as `agent.run_sync(...)` or `result.output`,
   counts by its last name, as long as the example uses something from the same top-level package.
   The verifier checks the full path on the real releases.
   The examples import names explicitly, without `import *`.
   With `rename.keyword_of`, it also covers a keyword argument whose name alone changed: same value, same meaning, same position.
   Then `example.before` passes the old keyword to one of the callables, and `example.after` passes the new keyword and not the old one.
   A call counts by the last part of its name, such as `Agent(...)` or `agent.run(...)`.
   For a `rename`, `example.after` is exactly `example.before` with the rename applied, and nothing else.
   The validator checks this when the rename stays in one module or renames a keyword argument.
   A move to another module will be checked once the engine can fix code.
   A changed string, such as the model prefix `grok:` becoming `xai:`, is a `signature` change with `instructions`.
4. **Tested examples.** `tools/verify_examples.py` installs real versions from PyPI
   and checks that `example.before` runs on the last stable release before `breaks_in` and fails on `breaks_in`,
   and that `example.after` runs on `breaks_in` and on `works_since`.
   When `works_since` is not later than that last stable release, `example.after` must run on it too.
   `example.before` must also run on the release just before `breaks_in`, even a pre-release,
   and `example.after` must fail on the release just before `works_since`, so that both are the first such releases.
   For a `rename`, the error that stops `example.before` must name the old name,
   and the error that stops `example.after` must name the new name.
   The paths of a `rename` must also resolve with `pkgutil.resolve_name`, with each release installed:
   `rename.from` on every release where `example.before` runs, but not on `breaks_in`,
   and `rename.to` on every release where `example.after` runs.
   So an old name that `breaks_in` keeps as a stub, which only raises when it is used, fails this check.
   For a renamed keyword argument, every callable in `rename.keyword_of` must resolve on every release where an example runs.
   It must also have `rename.to` as an explicit parameter on `breaks_in` and on `works_since`, as `inspect.signature` shows it.
   A callable that takes it only through `**kwargs` fails this check and doesn't belong in the list,
   because `example.after` could run on it while the argument is ignored.
   The verifier also checks the latest release after `breaks_in` that accepts the Python of the examples:
   the latest stable release, or the latest pre-release when `breaks_in` is a pre-release and no stable release follows it.
   There `example.before` must still fail and `example.after` must still run, and the paths and signatures are checked as on `breaks_in`.
   A failure there means that the library may have changed after `breaks_in`, for example by bringing the old name back.
   The latest release changes over time, so the result of this check depends on when it runs.
   CI runs it on the notices that a pull request changes, on every notice when the tools change,
   and on every notice on each push to `main` and every week.
5. **Offline examples.** Examples run without network access and with fake API keys,
   so they must not call real services: an example that fails after trying to reach the network fails the check.
   For a `behavior` change, use `assert` so that the example fails when the behavior differs,
   and `example.fails_with.before.error` is `AssertionError`.
6. **New notices start as `draft`.** A maintainer checks them against the sources and sets `verified`.
7. **IDs:** take the next free number, with four to eight digits and no extra leading zeros.
   A maintainer fixes any clash when merging.

## Changelog

- **0.3** (2026-10-25):
  - `affects` names the code that a change other than a rename breaks: paths,
    and keyword arguments that break when they are passed or when they are left out.
  - `example.fails_with` says how the examples fail. Every type but `rename` needs it, with `after` when `works_since` is set,
    and `after` needs `works_since` for every type. A `behavior` change fails with `AssertionError`.
  - `restored_in` is the first release in which the old code works again,
    and `withdrawn` marks a notice that should never have existed, whose ID stays taken.
  - IDs have four to eight digits, versions may have an epoch such as `1!2.0`,
    and `rename.from`, `rename.to` and the paths in `affects` may be a single name, such as a top-level module.
  - Every value except the example code is one line.
  - A `rename` moves to 0.3 by changing `schema_version`. Every other type also needs `affects` and `example.fails_with`.
    The schemas of 0.1 and 0.2 stay as they were.

- **0.2** (2026-10-23):
  - `rename.keyword_of` describes a renamed keyword argument, so that tools can fix it automatically.
  - A `rename` can't have `instructions`, and its `example.after` is exactly `example.before` with the rename applied.
  - IDs have no extra leading zeros: `BW-2026-0001` and `BW-2026-10000`, but not `BW-2026-00001`.
  - A notice without `works_since` means that the first release of the new code is unknown.
  - A notice moves to 0.2 by changing `schema_version`. The schema of 0.1 stays as it was.

- **0.1** (2026-10-21): first version. Changed the same day, before any other notice was added:
  notices are plain YAML, without anchors, aliases or tags, and IDs start with `BW-`
  (the project was renamed from bumpfix to Breakwise).
