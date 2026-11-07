# Architecture

Local Wiki Librarian converts an application-owned Markdown directory into an auditable, read-only retrieval runtime.

```text
Markdown root (read-only)
        |
        v
snapshot builder -- copies verified *.md files and records hashes
        |
        +--> authority compiler -- explicit current/authority/history/conflict tiers
        +--> packaged Wikimap index -- lexical candidates
        +--> graph adapter -- bounded Wiki-link expansion
        |
        v
authority-aware router -- filters/demotes unsafe tiers and follows redirects
        |
        v
Librarian Packet -- evidence, supporting paths, warnings, and stop reasons
```

## Authority model

Authority is data, not a filename or query heuristic. Only `authority: high` together with `status: active` creates a `current` entry. A high-authority page with a different status remains `authority`; all other unclassified pages remain `unknown` unless an explicit redirect/evidence/history/raw/index rule applies.

Application metadata such as `source_role`, `domain`, or `layer` is preserved for callers but never injects search results or promotes a page. The router ranks lexical and bounded-graph candidates; it does not contain application-specific people, schedules, task ledgers, collection names, or backend migration cues.

`catalog.md` is an optional routing projection. A missing catalog produces an empty valid projection; a present but malformed catalog remains an audit error. Core refresh, health, search, and audit therefore work for a plain Markdown tree without silently accepting a broken declared catalog.

## Safety model

- Canonical and state roots must be disjoint and must not traverse symlinks.
- Snapshot promotion is atomic and fails closed on stale, quarantined, deleted, or unreadable input.
- Conflict copies such as `name 2.md` are history/evidence and never outrank a canonical sibling.
- Packaged Wikimap bytes are checked against the bundled provenance manifest before use.
- Search results retain source path, authority tier, graph distance, freshness, and engine.
- Missing or degraded state produces an explicit warning/error rather than an unverified answer.

## Package boundary

The Python import namespace is `local_wiki_librarian`. Wikimap and its MIT license/provenance are package data under `_vendor/wikimap`; the Librarian Packet schema is under `schemas`. Installed wheels therefore do not depend on a source checkout layout.

The supported runtime is Python 3.9+ on Linux and macOS. POSIX file locking is required. This repository contains no real knowledge-base content, generated runtime state, credentials, or machine-specific paths.

## Filesystem failure boundary

Markdown reads, prior-generation fallback, and state-file reads use directory
file descriptors with no-follow traversal. Final files must be regular files and
are opened nonblocking, so a FIFO cannot hang a refresh. Static source symlinks
are excluded; a link or nonregular replacement after discovery is refused.
Discovery errors and unexplained missing inventory entries fail closed. Ordinary
file-read errors retain bounded retries and snapshot stale/quarantine behavior;
refresh requires a fully fresh candidate.

State writes use a unique temporary file descriptor, file fsync, atomic replace,
directory fsync, and descriptor-based readback. Pre-replace failure preserves the
old pointer. An error after replace is reported as an error even if the new
pointer is visible; both complete generations are retained, without automatic
rollback. Only the writer's own temporary file is removed. Existing predictable
temporary filenames are ignored. The shared no-follow regular-file lock preserves
the cooperative single-writer contract.

These checks protect source bytes, modes, mtimes, and inventory; atime is excluded.
They do not provide an atomic whole-source-tree view or isolation from an arbitrary
same-UID process that can rewrite owned state. Generation permissions are defense
in depth for cooperative writers, not an immutability guarantee.
