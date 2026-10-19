# Writing an MCP client for wwyd

This page is for authors of MCP clients and harnesses (auto-responders, voice assistants,
plugins) that call a wwyd server. It describes the parts of the contract you can rely on.
`SPEC.md` is the full design document; where the two disagree, `SPEC.md` wins and this page
is a bug.

The MCP endpoint is Streamable HTTP at `/mcp` (for example `https://wwyd.example.org/mcp`).
A local single-user server also speaks stdio (`wwyd stdio --user <login>`, owner scope, no
auth).

## Authentication

Pick one:

- **OAuth 2.1** (interactive clients). The server is its own authorization server:
  discover it through `/.well-known/oauth-protected-resource` and
  `/.well-known/oauth-authorization-server`. Register with **DCR** (`/register`) or use a
  **CIMD** client ID (an `https` URL of your client ID metadata document). PKCE with `S256`
  is mandatory. Public clients use `token_endpoint_auth_method: "none"`. Access tokens live
  1 hour; refresh tokens 30 days and rotate on every use (reusing a rotated refresh token
  revokes the whole grant, so store the new one atomically).
- **API keys** (scripts, headless harnesses). The owner creates a key on the account page;
  send it as `Authorization: Bearer wwyd_<prefix>_<secret>`.

### Scopes

| Scope | What the client can do |
|---|---|
| `owner` | every tool; the owner's own clients |
| `read` | `get_persona` only (personalising another assistant) |
| `respond:<a>,<b>` | `predict` only, and only for the listed audiences |

Request the scope in the OAuth `scope` parameter; the owner confirms it (and, for
`respond`, ticks the audiences) on the consent page. `tools/list` only shows the tools your
scope allows. `respond:` scopes name real audiences: `respond:default` is not a valid scope.
Renaming or deleting an audience revokes the grants and keys whose scope names it.

### Revocation

`POST /revoke` (RFC 7009, form-encoded):

- OAuth tokens: send `token` and your `client_id`. Public clients (`none`) need nothing
  else; confidential clients authenticate with their secret. A token is only revoked if it
  belongs to your client.
- API keys: send the key itself as `token`; no client authentication is needed.

The response is `200` whether or not something was revoked.

## Versioning

`serverInfo.version` in the MCP `initialize` result is a semantic version. Before 1.0,
breaking changes to tool shapes only bump the minor version (0.1 -> 0.2) and are listed in
`CHANGELOG.md`. Additive changes (new optional inputs, new output fields, new enum values
where this page says the list may grow) can appear in any release: **ignore unknown fields**
and treat unknown values as documented below.

## Errors

Tool failures come back as an MCP tool result with `isError: true` and structured content
`{"error": {"code", "message", ...details}}` (for example `field` for `invalid`). Codes:
`not_found`, `forbidden`, `invalid`, `conflict`, `too_large`, `rate_limited` (carries
`retry_after` in seconds), `spend_cap`, `llm_unavailable`. Messages are for humans; branch on
`code`.

`predict` is different: it does not fail for model or limit problems, it answers with
`type: "escalate"` and a code (below).

## Audiences and the `default` alias

An audience is a group of people the owner talks to (`work`, `family`, ...). Names match
`[a-z0-9][a-z0-9_-]{0,39}`. `list_audiences` (owner) returns
`{audiences: [{name, description, is_default, claim_count}]}`.

Every persona has exactly one default audience. Its name is the owner's choice (`everyone`
initially), so do not hard-code it: pass **`default`** wherever a tool takes an audience
name and the server uses the default audience, whatever it is called. No audience can be
named `default`. An unknown audience name in `predict` also falls back to the default
audience, but then `hints` says so; `default` itself produces no hint.

## `predict` (scopes `owner`, `respond`)

Input:

```
{
  situation: string (1..8000),        // the message or situation as the owner would see it
  audience: string,                   // audience name, or "default"
  facts?: [{ what: string, value: string | null, kind?: string }] (<= 20),
  kind?: "reply" | "decision",        // default "reply"
  include_private?: bool              // owner only
}
```

Output, one of:

```
{ type: "answer",   answer, confidence, disclosure, used_claims: [id], hints: [string] }
{ type: "needs",    needs: [{ kind, what, why }], used_claims: [id], hints }
{ type: "escalate", escalate: code, reason, hints }
```

- `answer`: for `kind: "reply"` the message text in the owner's style; for `"decision"` a
  short statement of what the owner decides. Show `disclosure` with any message you send on
  the owner's behalf.
- `escalate` codes: `low_confidence`, `out_of_persona`, `boundary`, `rate_limited`,
  `spend_cap`, `llm_unavailable`, `audience_not_permitted`. Hand the situation to the owner.

### Needs and the facts round trip

`needs` means a decision rule of the owner depends on facts the server cannot know. Each
need has:

- `kind`: `calendar.free_busy` (the owner's free/busy time, slots, other calls),
  `contact.relationship` (who the other person is to the owner) or `other`. The list may
  grow: **treat an unknown kind as `other`**.
- `what`: the fact to look up, in words, specific to the situation.
- `why`: which rule asked for it (`decision_rule clm_...`).

The server keeps no state. To continue, call `predict` again with the same `situation`,
`audience` and `kind`, plus `facts`: one entry per need, `what` copied from the need,
`value` the answer as text, `kind` optionally echoed. If you cannot find a fact out, send it
with **`value: null`**: the server then decides without it or escalates with
`low_confidence`, and never asks for that fact again. A client that keeps answering needs
therefore terminates; still cap the round trips (two is plenty).

```
-> predict {situation: "Call tomorrow at 3?", audience: "default"}
<- {type: "needs", needs: [{kind: "calendar.free_busy",
                            what: "owner's free slots tomorrow around 15:00", why: "..."}]}
-> predict {situation: "Call tomorrow at 3?", audience: "default",
            facts: [{what: "owner's free slots tomorrow around 15:00",
                     value: "14:00-16:00", kind: "calendar.free_busy"}]}
<- {type: "answer", answer: "3 works", ...}
```

Situations and facts go to the persona's LLM backend and are never stored or logged.

## `add_claims` (owner)

```
in:  { claims: [{ kind, category, text, audiences?: "all" | [name], visibility?: "shared" | "owner_only",
                  confidence?, evidence?, proposed_category?, status?: "pending" | "approved" }] (<= 200),
       absorb?: bool = true }
out: { created: [id], absorbed: [{ index, into }],
       dropped: [{ index, reason: "guard" | "previously_rejected" | "unknown_audience" | "invalid", detail? }] }
```

Claims land as `pending` for the owner's review; `status: "approved"` is honoured for owner
callers and makes them usable by `predict` at once. `category` must be an existing path:
read them with `list_categories` and create new ones with `upsert_category`. `audiences`
takes names from `list_audiences` (or `default`). Text must not contain emails, phone
numbers, URLs or handles.

## `list_categories` (owner)

Returns `{categories: [...]}`, the category tree (paths such as `work/meetings`, depth <= 3)
with descriptions, `claim_count` (claims directly in the node) and `total_claim_count`
(including sub-categories). `upsert_category {path, description?, rename_to?}` creates or renames one.

## `ingest_raw` (owner)

Sends raw material for background derivation; returns `{job_id, accepted}` at once, poll
`job_status {job_id}`. Raw records are never stored. 1-1000 records, <= 2 MB serialized.
Every record has `additionalProperties: false`: never send names, emails, phone numbers,
titles or locations. Datetimes are ISO 8601 (10-40 chars); without an offset they are read
in the persona's timezone. `audience` (optional) is an audience name or `default`; an
unknown one maps to the default audience and is counted in the job's `unknown_audiences`.

| `type` | Required fields | Optional fields |
|---|---|---|
| `message` | `from_owner: bool`, `text` (<= 4000), `ts`, `thread` (opaque batch-local label, 1-64 chars) | `audience`, `position: int` |
| `call_transcript` | `ts`, `turns: [{speaker: "owner" \| "other", text (<= 4000)}]` (<= 500) | `audience` |
| `calendar_event` | `start`, `end`, `attendees: int >= 0` (a count), `response: "accepted" \| "declined" \| "tentative" \| "organizer" \| "none"` | `recurring: bool`, `online: bool` |
| `decision` | `situation` (1-4000), `choice` (1-4000) | `audience`, `ts` |
| `note` | `text` (1-20000; the owner's own words about themselves) | `ts` |

`thread` only pairs a message from someone else with the owner's reply (to learn response
times) and is discarded with the batch.
