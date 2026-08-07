# Local Wiki Librarian

**Fail-closed, authority-aware retrieval for local Markdown knowledge bases.**

Local Wiki Librarian builds an immutable snapshot of an application-owned Markdown tree, compiles explicit authority metadata, searches it with a packaged Wikimap runtime, and returns structured evidence or a no-answer result.

## Features

- Read-only snapshotting with generation and SHA-256 tracking.
- Path traversal, symlink, root-overlap, and atomic-promotion checks.
- Explicit authority, redirect, conflict-copy, history, raw, and index tiers.
- Lexical retrieval with bounded Wiki-link graph expansion.
- Structured Librarian Packets, health/audit commands, rollback, and soak helpers.
- No hosted service, LLM provider, credentials, or private knowledge-base content.

## Platform support

The runtime supports **Python 3.9+ on POSIX systems** (Linux and macOS). It uses POSIX file locking for atomic local-state operations; Windows is not currently supported.

## Install from source

```bash
git clone https://github.com/JeremyDev87/librarian.git
cd librarian
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
python -m pytest -q
```

Inspect the commands with:

```bash
wiki-librarian --help
wiki-librarian-refresh --help
```

The import namespace is `local_wiki_librarian`; the distribution name is `local-wiki-librarian`.

## Authority contract

A page is eligible for the `current` tier only when its YAML frontmatter explicitly includes both:

```yaml
authority: high
status: active
```

A title, filename, directory, or application-specific `source_role` never grants authority. Missing or lower authority remains non-current. Redirects, evidence, history, raw files, index pages, and conflict copies are classified separately and cannot silently outrank explicit current truth.

An optional root-level `catalog.md` can add application routing metadata. Its absence is valid; if the file exists, malformed rows or missing owners fail audit rather than being ignored.

## Core pipeline

```text
Markdown root (read-only)
  -> verified snapshot
  -> explicit authority manifest + Wikimap index
  -> lexical candidates + bounded graph expansion
  -> authority/redirect filtering
  -> structured Librarian Packet or no-answer
```

See [`docs/architecture.md`](docs/architecture.md) and the [packet schema](src/local_wiki_librarian/schemas/librarian-packet.schema.json).

## Evidence boundary

The deterministic fixtures prove safety and package contracts, not universal search quality. Applications should supply owner-reviewed retrieval corpora, paraphrase/OOD and no-answer cases, and generation/rollback probes for their own Wiki.

## Security and license

Report vulnerabilities through the process in [`SECURITY.md`](SECURITY.md), never a public issue. The project is Apache-2.0. Packaged Wikimap code remains under its upstream MIT license and exact provenance in [`src/local_wiki_librarian/_vendor/wikimap/`](src/local_wiki_librarian/_vendor/wikimap/).
