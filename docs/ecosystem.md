# Building a harness that feeds wwyd

wwyd stops at the answer (SPEC §1). Reading messengers and calendars, classifying contacts,
transcribing calls and sending replies are the **harness's** job. This page is for people
writing such harnesses. Vendor facts were checked against the vendors' own documentation on
2026-10-18 unless marked *unverified*; APIs and plan limits change, so recheck before you
build on them.

> **Status:** `ingest_raw` (M4) and `predict` (M3) are being built. The shapes below follow
> SPEC §6 and may still change in details.

## 1. Where the data can come from

| Source | Owner's own history | Notes |
|---|---|---|
| **Telegram** | Telegram Desktop: *Settings → Advanced → Export Telegram data*, or *Export chat history* from a chat's menu; JSON or HTML ([blog](https://telegram.org/blog/export-and-more)). Programmatic: the MTProto client API with your own `api_id` ([obtaining api_id](https://core.telegram.org/api/obtaining_api_id), [`messages.getHistory`](https://core.telegram.org/method/messages.getHistory)). | The Bot API only receives updates sent to the bot; it cannot read your past chats ([Bot API](https://core.telegram.org/bots/api)). The export JSON format is not formally specified. |
| **WhatsApp** | *Export chat* in the app, one chat at a time, as a `.txt` file with or without media ([FAQ](https://faq.whatsapp.com/1180414079177245)). | The Business Platform (Cloud API) delivers new messages by webhook to business numbers only; it has no history endpoint ([Cloud API](https://developers.facebook.com/docs/whatsapp/cloud-api)). Message caps on export: *unverified*. |
| **Signal** | No export format or API. Backups (on-device or Secure Backups) are encrypted and only restorable into Signal ([backups](https://support.signal.org/hc/en-us/articles/10074659364122-Backups-and-Device-Transfers-on-Signal), [Secure Backups](https://support.signal.org/hc/en-us/articles/9708267671322-Signal-Secure-Backup)). | A harness can only see messages it handles itself going forward. |
| **Slack** | [`conversations.history`](https://docs.slack.dev/reference/methods/conversations.history) with `channels:history`, `groups:history`, `im:history` or `mpim:history`. Admins/owners can export workspace data ([help](https://slack.com/help/articles/201658943)); which conversations are included depends on the plan (*unverified*). | Since 2025-05-29, new commercially distributed non-Marketplace apps get 1 request/min and 15 messages per call on this method; internal apps keep higher limits. |
| **iMessage** | No public API. Apple documents Messages in iCloud only as sync ([support](https://support.apple.com/en-us/102203)). | `~/Library/Messages/chat.db` on macOS is undocumented, needs Full Disk Access, and its schema may change with any macOS update. |
| **Gmail / email** | Gmail API [`users.messages.list` / `get`](https://developers.google.com/workspace/gmail/api/reference/rest/v1/users.messages) with `gmail.readonly`; [Google Takeout](https://support.google.com/accounts/answer/3024190) exports mbox; any provider via [IMAP](https://www.rfc-editor.org/rfc/rfc9051). | The owner's sent mail is the useful part (`said`; the mail it answers is `situation`). Strip quoted replies and signatures. |
| **Google Calendar** | [Events](https://developers.google.com/workspace/calendar/api/v3/reference/events) via `events.list` (`calendar.readonly`). The owner's response is `attendees[].responseStatus` on the entry with `self: true` (`needsAction`, `declined`, `tentative`, `accepted`); `organizer.self` marks events the owner organised. | Response status is not propagated for events with more than 200 guests. |
| **Outlook calendar** | Microsoft Graph [`GET /me/calendarView`](https://learn.microsoft.com/en-us/graph/api/user-list-calendarview) (least privilege `Calendars.ReadBasic`); each event has `responseStatus.response` and `isOrganizer` ([event](https://learn.microsoft.com/en-us/graph/api/resources/event)). | `calendarView` expands recurring events into occurrences; `type` tells you whether an event is recurring. |

## 2. Contacts → audiences (on the harness side)

wwyd never receives who someone is (SPEC §2, P2). The harness keeps its own mapping from a
contact to one of the owner's **audience names** (`list_audiences`), e.g. `family`,
`colleagues`, `clients`, `unknown`. Store that mapping in the harness, never in wwyd.
Send only the audience name. Unknown names fall back to the default audience.

## 3. Building `ingest_raw` episodes (SPEC §6.1)

Up to 1000 episodes or 2 MB per call; every episode rejects unknown fields. One shape
covers every source; the domain goes into `category` (see `docs/clients.md` for the full
schema and limits).

```json
{"episodes": [
  {"when": {"start": "2026-10-01T09:12:00Z", "end": "2026-10-01T09:40:00Z"},
   "audience": "colleagues", "category": "communication/chat",
   "situation": "Can we move the sync to Thursday?", "said": "Thursday works, 10:00?"},
  {"when": {"start": "2026-10-02T10:00:00+02:00", "end": "2026-10-02T10:30:00+02:00"},
   "where": "online", "category": "work/meetings",
   "situation": "recurring invite, 3 attendees", "decision": "accepted"},
  {"category": "work/talks", "situation": "Invited to speak at a weekend meetup",
   "decision": "declined, offered a recorded talk"},
  {"category": "hobbies/films", "when": "2026-10-03T21:00:00+02:00",
   "decision": "watched to the end, 4/5"}
]}
```

- A chat reply is one episode: the incoming message(s) as `situation`, the owner's reply
  as `said`; `when` from the first incoming message to the reply gives reply latency. Do
  not use the same category for meeting spans and reply intervals.
- A calendar event becomes a choice: describe the invite in `situation` by role and size
  ("1:1 with a client", "invite, 12 attendees"), never by title or attendee names; the
  response goes to `decision`.
- A call: what the others said in `situation`, the owner's turns in `said`.
- Something the owner wrote about themselves: `said` only.
- `where` is coarse context (`home`, `online`, `work Slack`), never an address or a room.
- Category paths are labels you choose (`hobbies/films`, `кино`): unknown ones are
  created. Keep names out of them (`family/sister`, not `family/anna`).
- Use timezone-aware timestamps; analyzers care about local time.
- `ingest_raw` returns at once with a `job_id`; poll `job_status`. Derived claims arrive as
  `pending` for the owner to review.

## 4. Call transcription

wwyd does not transcribe. Options:

- **Local**, nothing leaves the machine: [OpenAI Whisper](https://github.com/openai/whisper)
  (open source, MIT) and Whisper-class derivatives. Needs diarisation (who spoke) to split
  the owner's turns (`said`) from the others' (`situation`).
- **Cloud**: e.g. [OpenAI speech-to-text](https://platform.openai.com/docs/guides/speech-to-text),
  [Google Cloud Speech-to-Text](https://cloud.google.com/speech-to-text/docs). The audio
  and transcript go to that provider; recording calls may need the other party's consent
  where you live.

## 5. What never to send

- Names, phone numbers, emails, usernames, messenger or chat IDs, or hashes of any of them —
  not in any field and not in category paths. Replace names in text with a role (`[colleague]`)
  before sending. The server's verbatim guard is a safety net, not a filter to rely on.
- Calendar titles, descriptions, attendee lists, locations, meeting links (the schema has
  no fields for them; do not smuggle them into `situation`, `decision` or `where`).
- Attachments, media, passwords, codes, financial or health data, or third parties'
  messages that are not part of an exchange with the owner.
- Raw text goes to the persona's LLM backend for derivation; with a cloud backend the
  provider sees it. Set `llm_for_ingest: false` for code-only derivation.

## 6. Using `predict` (SPEC §8)

```json
{"situation": "Message from a colleague: 'Can we meet Tuesday about the budget?'",
 "audience": "colleagues", "kind": "reply"}
```

The result is one of:

- `{"type": "answer", "answer", "confidence", "disclosure", ...}` — show `disclosure`
  together with the answer when it goes to a third party (EU AI Act Art. 50). For drafts the
  owner sends personally, the harness decides.
- `{"type": "needs", "needs": [{"what": "free slots on Tuesday ≥ 20 min", "why": "decision_rule clm_…"}]}`
  — look the facts up (e.g. in the calendar) and call again with the same situation plus
  `"facts": [{"what": "free slots on Tuesday", "value": "10:00–11:00, 15:00–15:30"}]`.
  The server keeps no state between the calls; facts are used once and never stored.
- `{"type": "escalate", "escalate": "low_confidence"|"boundary"|"rate_limited"|…}` — do not
  answer automatically; notify the owner.

Use a `respond:` scoped key for auto-responders so the harness can only answer for the
audiences the owner allowed.
