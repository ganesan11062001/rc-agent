# rc-kg HTTP API contract — v1 (current: 1.2)

What clients (TicketMate's `backend/app/kg_client.py` today) may rely on.
`GET /health` returns `api_version`; clients check the **major** number.

- **MAJOR** goes up when an existing client could break: a field below is
  removed, renamed or changes meaning, or a request that worked is rejected.
- **MINOR** goes up for additions: new optional request fields, new response
  fields, new endpoints. Clients must ignore fields they do not know.

`scripts/contract_test.sh` checks a live instance against this file; run it
before tagging a release. Every field listed here is asserted there.

## Auth

If the server has `RC_KG_API_TOKEN` set, every endpoint except `/health`
needs `Authorization: Bearer <token>`; a wrong or missing token is `401`.

## `GET /health`

Never needs the token. Always `200`.

| field | type | |
|---|---|---|
| `status` | `"ok"` \| `"loading"` \| `"failed"` | only `"ok"` means queries will be answered |
| `api_version` | string, `"MAJOR.MINOR"` | |
| `detail` | string \| null | when not `"ok"` |
| `sections`, `embedded`, `retrievable` | int | when `"ok"` |
| `snapshot` | `{built_at, git_commit, eval, variant, sites}` \| null | when `"ok"`; `variant` is `"full"` (website + docs) or `"website"` (1.2) |

## `POST /retrieve`

Request: `{"query": str (1–50,000 chars), "k": int 1–20 = 6, "include_events": bool | null}`

`503` while loading; `400` for an empty query.

| field | type | |
|---|---|---|
| `query` | string | |
| `query_topics` | string[] | |
| `low_confidence` | bool | best match is weak; say so rather than cite |
| `context` | string | numbered `[S1]…` block to put in a prompt |
| `allowed_urls` | string[] | every URL an answer may contain |
| `sources[]` | object[] | in rank order |
| `sources[].ref` | `"S1"`, `"S2"`, … | |
| `sources[].url` | string | exact section link — what gets cited |
| `sources[].page_url` | string | |
| `sources[].page_title` | string | |
| `sources[].heading` | string | |
| `sources[].text` | string | |
| `sources[].published` | string | `YYYY-MM-DD`, may be empty |
| `sources[].last_modified` | string | `YYYY-MM-DD`, may be empty (1.1) |
| `sources[].site` | string | `rc.northeastern.edu` or `rc-docs.northeastern.edu` (1.1) |
| `sources[].score` | number | |

## `POST /cite`

Request: exactly one of `text: str` or `texts: str[]`, plus
`refs: {"S1": url, …}`, `allowed_urls: str[]`,
`style: "markdown" | "plain" | "numbered" = "markdown"`,
`check_support: bool = false`.

| field | type | |
|---|---|---|
| `text` / `texts` | as sent | markers expanded, unretrieved URLs replaced by `[link removed]` |
| `cited` | string[] | refs used, first-use order |
| `references` | `{n, ref, url}[]` | `numbered` style; one numbering across all `texts` |
| `removed_urls` | string[] | |
| `unknown_refs` | string[] | markers naming no source |
| `support` | `{ref, claim, verdict, score, best_ref, best_score}[]` \| null | `check_support` only, and only once loaded; `verdict` is `"ok"` \| `"better_source"` \| `"weak"` |

## Changes

- **1.2** (2026-10-23): `snapshot.variant` and `snapshot.sites` — which
  graph is being served (`full` or `website`).
- **1.1** (2026-10-22): `sources[].site` and `sources[].last_modified`, for
  the rc-docs site. `context` labels docs sources and their update date.
- **1.0**: initial contract.

## `POST /answer`

Optional (needs `LLM_BASE_URL` on the server, else `503`). Not used by
TicketMate; not covered by this contract beyond existing.
