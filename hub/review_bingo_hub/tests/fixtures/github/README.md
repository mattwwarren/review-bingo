# GitHub fixtures

Two kinds of file live here, and the difference matters more than it looks.

## Captured (redacted, never tidied)

- `pull_request_closed.json` — a real GitHub App webhook delivery from
  2026-08-04, with the installation id redacted. Kept verbatim: the nulls, the
  odd field ordering, and the fields we do not read are the whole point. A
  payload we invent agrees with whatever we already believe about GitHub's
  shape, so it can never contradict us.

## Synthesized (hand-built — treat their shape as a hypothesis)

These three were **hand-built from GitHub's REST documentation**, not captured
against a live token, because obtaining one requires completing the device flow
against a real App installation:

- `user_identity.json` — `GET /user`
- `user_installations.json` — `GET /user/installations`
- `installation_repositories.json` — `GET /user/installations/{id}/repositories`

They carry only the fields `github_identity_service` actually reads
(`id`/`login`; `installations[].id`/`app_id`; `repositories[].full_name`/
`permissions`), plus enough neighbours to keep the envelope shape honest
(`total_count`). Because they encode our belief rather than an observation,
they cannot falsify that belief — they pin the mapping logic (permission
collapse, app-id filtering, pagination), not GitHub's contract.

**When a real capture becomes available, replace them and delete this
paragraph.** Until then, the strict parsing in `github_identity_service.py` is
the sensor: a `KeyError`/`GithubIdentityError` in production against real
traffic is a reading about where our hypothesis was wrong, not a nuisance to
widen away.
