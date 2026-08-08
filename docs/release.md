# Release runbook

이 저장소의 배포 자동화는 **candidate freeze**, **GitHub Release**, **PyPI publish**를 서로 다른 게이트로 유지합니다. 이 문서는 절차를 고정하지만 tag/Release/PyPI를 자동으로 실행하거나 승인하지 않습니다.

## 1. Candidate freeze

1. 공개 `main`의 exact commit SHA를 readback합니다. 기본 브랜치가 이동하면 기존 candidate를 폐기하고 새 SHA로 다시 시작합니다.
2. GitHub Actions의 **Release candidate**를 수동 실행하고 `source_sha`와 현재 프로젝트 version에 대응하는 tag(현재 release는 `v0.1.0`)를 입력합니다.
3. workflow가 trusted `main`을 checkout한 뒤 SHA를 비교하고, 한 번의 `python -m build` 결과만 검증하는지 확인합니다.
4. `twine check`, wheel/sdist 격리 verifier(각 2회), `release-manifest.json`의 source/tree/artifact digest/provenance를 확인합니다.
5. candidate artifact는 검토용입니다. 이 workflow에는 `id-token: write`, `contents: write`, release 생성, publish 권한이 없습니다.

`release-manifest.json`은 다음을 고정합니다.

- distribution version, tag, source commit/tree
- wheel/sdist SHA-256, size, archive member list
- vendored `UPSTREAM.json`, Wikimap file hashes, producer script hashes
- verifier JSON receipts
- root `NOTICE` 정책

### NOTICE 정책

현재 결정은 **root `NOTICE`를 wheel/sdist에 넣지 않는 것을 non-blocking으로 허용**하는 것입니다. 대신 vendored `LICENSE`와 `UPSTREAM.json`은 package-data에 포함하고 manifest/verifier에서 hash를 확인합니다. 이 정책을 바꾸면 package/security 전체 검증을 다시 실행해야 합니다.

## 2. GitHub Release Owner Gate

candidate를 사람이 검토한 뒤에만 별도 Owner Gate로 GitHub Release를 생성하고, 아래 파일을 같은 Release에 첨부합니다.

- `release-manifest.json`
- 정확히 한 개의 `local_wiki_librarian-0.1.0-*.whl`
- 정확히 한 개의 `local-wiki-librarian-0.1.0.tar.gz`

Release는 published/non-prerelease 상태여야 합니다. tag는 exact source commit을 가리켜야 하며, 첨부 artifact digest는 manifest와 일치해야 합니다.

## 3. PyPI Trusted Publishing Owner Gate

1. **Owner Gate:** PyPI project `local-wiki-librarian`과 GitHub Environment `pypi`의 Trusted Publisher 설정을 별도로 확인합니다. Environment에는 required reviewers를 지정하고, 배포 branch restriction은 `main`으로 제한하며, 실제 settings 화면/API readback 증거를 보관합니다.
2. publish workflow를 `tag`와 `source_sha`로 수동 실행합니다.
3. admission job은 trusted default-branch helper로 tag 형식·tag→commit·published Release·manifest·wheel/sdist digest를 검증합니다.
4. publish job은 다시 Release asset을 내려받아 동일 admission과 `twine check`를 통과한 뒤에만 `pypa/gh-action-pypi-publish`를 호출합니다.
5. OIDC 권한은 publish job에만 있고, 환경 승인(`pypi`)을 통과해야 합니다.

PyPI 버전은 재사용할 수 없으므로 publish 직전 아래를 최종 readback합니다.

- manifest source SHA/tree와 tag
- wheel/sdist SHA-256 및 metadata
- package member/provenance
- `NOTICE` 결정과 vendored license

## 4. 사후 검증 및 rollback 경계

publish 후 별도 isolated environment에서 PyPI 설치, `wiki-librarian`/`wiki-librarian-refresh` CLI, resource/schema 발견, vendored hash, 2-generation source-invariance smoke를 실행하고 PyPI metadata/digest를 다시 읽습니다.

결함이 발견되면 이미 publish된 버전을 삭제·재사용하지 않습니다. 문제 버전은 yank 여부를 Owner가 결정하고, 수정된 다음 버전을 새 candidate로 만들어 다시 freeze합니다. tag, GitHub Release, PyPI publish, Trusted Publisher 설정, merge는 이 local-only loop의 수행 범위가 아닙니다.
