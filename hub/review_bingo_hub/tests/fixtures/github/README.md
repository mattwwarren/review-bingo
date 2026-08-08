# GitHub fixtures

Two kinds of file live here, and the difference matters more than it looks.

## Captured (redacted, never tidied)

- `pull_request_closed.json` — a real GitHub App webhook delivery from
  2026-08-04, with the installation id redacted. Kept verbatim: the nulls, the
  odd field ordering, and the fields we do not read are the whole point. A
  payload we invent agrees with whatever we already believe about GitHub's
  shape, so it can never contradict us.

## Synthesized (hand-built — treat their shape as a hypothesis)

These were **hand-built from GitHub's REST documentation**, not captured
against a live token, because obtaining one requires completing the device flow
against a real App installation:

- `user_identity.json` — `GET /user`
- `user_installations.json` — `GET /user/installations`
- `installation_repositories.json` — `GET /user/installations/{id}/repositories`
- `device_code_grant.json` — `POST https://github.com/login/device/code`
- `token_poll_{success,pending,slow_down,expired,denied}.json` —
  `POST https://github.com/login/oauth/access_token`, one file per answer the
  device flow can give

The device-flow six carry the same disclosure and the same blocker as the three
above — capturing them means completing a live OAuth handshake, which is the
very thing the fixtures stand in for. Their shape is mirrored from
`client/test_bingo_client.py`, which has encoded the same handshake since A1
(#20): the CLI and the hub broker one flow, so a hub-side fixture that
disagreed with the CLI's would hide exactly the drift worth catching.

They carry only the fields `github_identity_service` actually reads
(`id`/`login`; `installations[].id`/`app_id`; `repositories[].full_name`/
`permissions`; `device_code`/`user_code`/`verification_uri`/`expires_in`/
`interval`; `access_token`/`error`/`interval`), plus enough neighbours to keep
the envelope shape honest (`total_count`, `error_description`/`error_uri`).
Because they encode our belief rather than an observation,
they cannot falsify that belief — they pin the mapping logic (permission
collapse, app-id filtering, pagination), not GitHub's contract.

**When a real capture becomes available, replace them and delete this
paragraph.** Until then, the strict parsing in `github_identity_service.py` is
the sensor: a `KeyError`/`GithubIdentityError` in production against real
traffic is a reading about where our hypothesis was wrong, not a nuisance to
widen away.
