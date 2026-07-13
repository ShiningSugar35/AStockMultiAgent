---
name: knowledge-ingest
description: Ingest an approved local document or allowlisted author's accessible history with immutable snapshots and resumable coverage. Use for the local investment book, approved PDFs, Zhihu answers, thoughts, articles, author-participating comment chains, collection coverage, retries, or incremental knowledge updates.
---

# 知识采集

1. Run `uv run astock probe`, then confirm the source is local private material or an allowlisted author the user can access.
2. Prefer a verified structured request, then MCP, then the logged-in browser, then manual HTML/Markdown.
3. Save every raw response to ObjectStore before advancing a checkpoint.
4. Enumerate each content type from its first page to an explicit terminal condition.
5. Record failures and gaps separately from confirmed empty results.
6. Pause on 403, 429, login loss, security verification, or unexpected structure.
7. Keep full raw comments, then derive only the branches required to understand the target author's participation.

## Output

Produce `AuthorCollectionCoverageReport` for every author/content type. Later distillation must produce separate candidate-selection and position-lifecycle manifests with source snapshot IDs.

## Prohibitions

- Do not store plaintext cookies or browser profiles in Git or logs.
- Do not bypass captchas, signatures, access controls, or rate limits.
- Do not interpret a failed or empty response as proof that no content exists.
