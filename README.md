# Prior authorization API

Proprietary utilization-management service. A provider office submits a future procedure, a reviewer decides each line, and the database can show who did it.

Synthetic people only. Do not load real member, patient, or provider data.

## How to run

Requirements: Python 3.12 and Docker (or any Postgres 16 you can reach).

```bash
docker compose up -d
python3 -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/alembic upgrade head
.venv/bin/python -m app.seed
.venv/bin/uvicorn app.main:app --reload
```

Postgres listens on `127.0.0.1:54329`. The API listens on port 8000. Interactive docs are at http://127.0.0.1:8000/docs.

`python -m app.seed` prints a bearer token for each synthetic user. Send it as `Authorization: Bearer <token>`. Those tokens stand in for the company identity service. They carry a user id and a role, and they are checked against the user row on every call.

```bash
curl -s http://127.0.0.1:8000/health
curl -s http://127.0.0.1:8000/v1/me -H "Authorization: Bearer <token>"
```

Copy `.env.example` to `.env` to change the database URL, the token secret, or the optional documentation-model endpoint.

Tests use a separate database, `priorauth_test`, on the same server:

```bash
docker compose exec postgres psql -U priorauth -c 'CREATE DATABASE priorauth_test'
.venv/bin/pytest
```

`pytest` fails unless `app/` is at 100% line and branch coverage.

The Dockerfile is the shape for AWS (a container in front of RDS). It is not required to run the tests.

## What the API does

| Who | Can |
| --- | --- |
| Provider office | Submit for a member linked to that practice. See their own requests, including the note they wrote and the denial reason. |
| Member | See their own requests and whether each procedure was approved. |
| Reviewer | See the unassigned queue and the cases they hold. Pick up a case, then approve or deny every line in one submission. |
| Operations admin | See everything. Hand a case to another reviewer. See reviewer load and the unsent decision notices. |
| Support | Look up submission attempts, including ones we rejected. See status only, not the note or the reason. |

Partners should send `Idempotency-Key` on `POST /v1/auth-requests`. The same key and the same body return the original result. The same key and a different body is a conflict. A rejected submission is stored, so support can answer "it didn't go through" with the problem list.

A denial needs a reason code and a sentence the office can read. An approval cannot carry a denial reason. Decisions are final.

## Decisions

**Date of service is a calendar date in the practice's timezone.** Maine and Hawaii do not share a "today." A date before today in that zone is retro review, which this service does not do. A date more than one calendar year out is rejected. The anniversary itself is accepted. Feb 29 rolls to Feb 28. The same calendar day is accepted even if the appointment time has already passed, because we do not store a time of day.

**Coverage ends on `terminated_on` inclusive. A practice link ends on `unlinked_on` exclusive.** Both are written next to the tables.

**More than 30 units is rejected unless the office sends `confirm_high_units: true`.** More than 200 is rejected even then. Thirty is allowed without confirmation.

**One ICD-10-CM diagnosis, shaped but not looked up in a code set.** We were not given a code set. CPT is five digits. HCPCS is one letter and four digits. Modifiers go in the note. A repeated code is rejected; the count belongs in `units`.

**A reviewer who currently treats that member cannot pick up or decide the case.** A closed relationship does not block. "Currently" is what the requirement says. A lifetime ban would be a one-line change in `eligibility.py`.

**Another reviewer's open case is a 404, not "already assigned."** We do not tell a caller that a chart they cannot see exists.

**Members do not see the clinical note, the denial reason, the reviewer's name, or the documentation briefing.** Offices see the denial reason because they have to act on it. The internal comment stays with the reviewer and the admin. Lists never include the note. Opening a chart that includes the note writes an audit row.

**An approved, denied, or partial request must have `decided_by` and `decided_at`.** That is a database constraint (`ck_decided_request_has_actor`), not only an application check. A denied line must have a reason code and non-blank text. An approved line cannot have denial text.

**Support is a role even though the UM note described a desk rather than a user type.** They get the attempt log and status, which is the minimum they need. Auditors are the next group the team named. An unknown role sees nothing until `policy.py` learns it. Add the role in `can_see_request` and `list_clause` in the same change.

**Decision notices are rows in `notification_outbox`.** This process does not send email. The payload has no clinical note. A worker with a BAA should drain the table.

**The documentation check never approves or denies.** See below.

**Submission attempts keep the member id the office sent, with no foreign key.** Support has to be able to find a typo. The clinical note is not copied onto the attempt. The body hash is enough to recognize a retry.

**Tokens are HMAC, not a JWT library.** There is no algorithm header. The role in the token must still match the user row.

## How AI was used

Cursor, with a coding agent, wrote the first draft of this service from the challenge text. The agent was told to use Python, Postgres, and a documentation model, and to keep the result proprietary.

Three things in that draft were wrong and did not stay:

- The model was on the path that sets `approved` or `denied`. A model does not get a vote. `app/ai.py` checks the note for the diagnosis and the procedure codes, and that is all. If a remote completion contains decision language (`approve`, `deny`, `reject`, "not medically necessary"), the text is thrown away and not stored. The reviewer still gets the in-process checklist, marked `output_rejected`.
- The member payload included the denial sentence. That is reviewer reasoning. Members see the line status only.
- "Already happened" was computed in UTC. A Hawaii office would have been judged by a Maine calendar. The date is evaluated in the practice zone.

The remote model is off unless `PRIOR_AUTH_AI_BASE_URL` is set. Leave it empty unless that endpoint is covered by a BAA. A failed or rejected model call does not fail the submission. The office's request is already in the queue.

The test suite includes a scripted model that says "APPROVE" and asserts the stored narrative does not.

## Where to read the code

| File | Why it is here |
| --- | --- |
| `app/models.py` | Tables, including the constraint that an approval has an author |
| `app/validation.py` | Every office-facing rejection message |
| `app/eligibility.py` | What "active on this date" and "currently treats" mean |
| `app/service.py` | Submit, pick up, decide, reassign, each in one transaction |
| `app/visibility.py` | Which fields each role receives |
| `app/policy.py` | Where a new role has to be added |
| `app/ai.py` | Advisory briefing and the output guard |
| `app/auth.py` | The stand-in identity token |
| `app/api.py` | HTTP |
| `tests/` | The rules above, including the edges |

## License

All rights reserved. See `LICENSE`. Oxbridge Health may read and run this privately to evaluate the application. No other license is granted.
