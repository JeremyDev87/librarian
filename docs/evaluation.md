# Retrieval evaluation

`scripts/evaluate-wiki-retrieval.py` measures the existing authority-aware router on an inline, constructed public corpus. It does not change the search policy or use an existing Wiki or runtime state. A completed measurement can contain misses: completion is not a search-quality approval.

## Run from an installed development environment

From the repository root, use Python 3.9 or newer on Linux or macOS:

```bash
python -m venv .venv
. .venv/bin/activate
python -m pip install -e '.[test]'
REPORT_DIR="$(mktemp -d)"
python scripts/evaluate-wiki-retrieval.py \
  --corpus tests/fixtures/retrieval-seed.json \
  --output "$REPORT_DIR/seed-baseline.json"
python -m json.tool "$REPORT_DIR/seed-baseline.json"
```

The evaluator has only `--corpus` and `--output` options. The package must be installed in the selected Python environment, including for the separate public CLI checks. The output parent directory must exist; the output file must be new. Existing files and symlink outputs are refused rather than overwritten. Keep the report outside the source tree and use a new path for another run.

The evaluator creates its own temporary source and state trees, refreshes once, verifies the bundled vendor and snapshot, and checks positive, negative, and field-search cases through a separate `python -m local_wiki_librarian.cli` process against the direct router. It then measures that prepared runtime without refreshing between queries. Source bytes, modes, mtimes, and inventory are compared before and after; atime is excluded. Temporary source/state trees are removed when the run exits; verified views are owned by the evaluator process and removed at process exit. The requested JSON report remains for inspection.

## Corpus contract and provenance

The UTF-8 JSON input uses `schema_version: 1` and the current `corpus_id: "librarian-public-seed-v1"` contract. Its top-level fields are:

| Field | Meaning |
| --- | --- |
| `source_revision` | Public repository revision used to prepare the seed. |
| `provenance` | Records containing `document_path`, `source_path`, `section`, `source_sha256`, `kind`, and `description`. |
| `documents` | Inline Markdown records with `path` and `content`. No external source-file loading is supported. |
| `queries` | Predeclared questions, labels, splits, and comparison variants. |

Each query has a unique `id`, a `family`, a `split` (`dev` or `frozen_check`), `query`, `should_not_return`, `label_basis`, and `variants`. It declares exactly one of nonempty `expected_paths` or `expected_no_answer: true`. Expected and forbidden paths must name included documents and must not overlap. Paths are relative, normalized, and confined to the generated source tree; absolute paths, parent traversal, and case/Unicode-alias document duplicates are rejected. A family belongs to one split.

The checked-in seed contains six documents and eighteen questions covering platform support, snapshots, authority, packaging, out-of-domain questions, and query syntax. Four documents are Korean restatements of public repository material. Two are explicitly synthetic adversarial controls: an unverified raw draft and a document marked `do_not_answer_as_current`. Their metadata is evaluation data, not real user authority. Provenance identifies the supporting public file, revision, section, and checksum; the evaluator checks its schema, not the historical source claims themselves.

Labels and the four polite-question variants were reviewed and frozen before retrieval. `label_basis` records supporting sections or why the corpus cannot answer. Variants are for the separate native comparison; the operational baseline submits only each original `query`. Splitting this small, constructed corpus into `dev` and `frozen_check` does not make it an independent real-user holdout. Changing documents, labels, or variants after seeing results requires a new corpus revision and a new evaluation; old results cannot establish a fair comparison against changed inputs.

## Report and measurement scope

The JSON report binds results to the corpus SHA-256, package version, vendor SHA-256, Python/SQLite/OS versions, generation and current-pointer identity, and source inventory. Per-query identifiers and query/label/variant hashes connect rows to the input without copying raw questions or full document bodies into the report. Preserve the input and record the source commit separately when comparing runs.

The router uses `k=5`, graph depth `1`, and `include_zero_score_graph=False`, with one query at a time. Each query has one warm-up followed by five measured calls. Samples contain milliseconds from `perf_counter_ns` around the whole `router.search` call. Index preparation, CLI startup checks, and warm-up are excluded. Median and nearest-rank p95 are reported per query; with five samples, p95 is the observed maximum. These are local observations, not population latency estimates or an operational SLA. Results must remain stable across warm-up and measured calls.

Each completed row includes ranked paths; result tier, source, and graph distance; adapter `weak`, `partial`, and term-frequency diagnostics; normalized router warnings; scores; forbidden paths; and latency samples. Hashed terms can be connected to the input without exposing their text. A weak/partial marker describes that call, not a proven root cause. For example, an explicit `dead_vocabulary` warning supports an abstention diagnosis; absent evidence should remain `unknown` rather than imply an unmeasured retrieval failure.

The existing scorer supplies the metrics unchanged:

| Metric | Interpretation |
| --- | --- |
| Recall@5 | Fraction of expected paths returned in the top five. |
| MRR | Reciprocal rank of the first expected path under the existing scorer. |
| Top-1 accuracy | Whether the first returned path is expected. |
| Authority false-positive count/rate | Queries returning at least one authority-ineligible path in the top five. Ineligible means outside `current`/`authority`, or explicitly blocked from answering as current. |
| `forbidden_return_count` | Number of queries with at least one `should_not_return` hit; not the total number of forbidden documents. |

For a no-answer label, correct empty results score one for recall, MRR, and top-1; returning a candidate scores zero. Thus combined answerable/no-answer averages include correct abstentions. Inspect `aggregate.answerable` and `aggregate.no_answer` separately, alongside `aggregate.all`, `aggregate.dev`, and `aggregate.frozen_check`. Authority eligibility and the explicit forbidden list are separate checks; neither should be inferred only from an answer hit.

## Misses and execution failures

A completed answerable query is `hit` when at least one expected path is retrieved and `miss` otherwise; consult recall for partial coverage of multiple expected paths. Empty results for an answerable query indicate over-abstention. A no-answer row is `abstained` when empty and `wrong_candidate` otherwise. Forbidden results are recorded separately even when a row hits the expected answer.

A miss is a measurement and does not stop subsequent questions. A search exception produces `status: "invalid"`, `outcome: "execution_error"`, null diagnostics and score, and an exception class/reason code. Previous successful diagnostics must not be interpreted as belonging to that failure. Later questions are still attempted. Preparation failures have no successful measurements; affected rows are marked invalid.

Exit `0` with report `status: "complete"` means all calls completed, even if retrieval scores are low. Exit `2` means invalid input/output or invalid execution. For an invalid measured run, `aggregate` is null; partial successful rows are not a successful average. Invalid input or an unusable output path may produce no report, so inspect the process exit and stderr as well. Generation drift, index-integrity failure, nondeterministic results, or changed source bytes invalidate the run.

## Separate native multi-query comparison

The evidence-only comparison uses all four predeclared polite-question pairs. Both arms call the same bundled Wikimap CLI against one preverified snapshot and exact index, with `k=5`: the first submits the original query alone, and the second adds its single frozen variant to invoke native reciprocal-rank fusion (RRF). The vendor performs fusion; no replacement fusion implementation is used. Labels are used only by the existing scorer after results are collected.

This compares **raw vendor retrieval**, which does not apply the operational router's authority or abstention gates. Keep its rows and metrics separate from the operational baseline. A raw blocked/draft hit is a raw-vendor observation, not proof that the operational router returned it. Verify query identity and corpus, vendor, index, and snapshot identities before accepting a comparison; mismatches invalidate it.

A result is `research_candidate_only` only when every pair has nondecreasing recall@5, MRR, and top-1, at least one metric strictly improves for at least one pair, and forbidden hits never increase. Otherwise it is `no_candidate`. An environment-blocked comparison is `inconclusive`, not an improvement. Every branch retains `production_decision: "retain_baseline"`.

This comparison does not authorize production RRF, automatic rewriting, changed abstention thresholds, or authority promotion. Follow-up work needs permissioned real questions and document snapshots, owner-reviewed answer/no-answer/forbidden labels, independently frozen evaluation cases, and evidence of the specific observed failure. Prioritize only causes supported by runtime diagnostics and source facts. Until those data exist, keep the current search policy and do not claim real-user quality improvement from the constructed seed.
