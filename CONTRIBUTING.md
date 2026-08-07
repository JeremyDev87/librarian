# Contributing

Thank you for helping improve Local Wiki Librarian.

## Development

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
python -m ruff check .
python -m compileall -q src scripts tests
```

Changes to authority ordering, path safety, snapshot promotion, redirect handling, no-answer behavior, package data, or console entry points require regression tests. Before review, also build the wheel and sdist and smoke-test the installed wheel in a fresh environment.

## Scope and privacy

Keep the project focused on local, read-only Markdown retrieval. Do not commit personal knowledge-base documents, runtime state, credentials, provider tokens, generated indexes, machine-specific absolute paths, or application-specific people/roles/policies.

Do not modify packaged Wikimap bytes without updating and independently verifying its upstream provenance, checksum, license, and notice. Report security issues according to [`SECURITY.md`](SECURITY.md).
