# Telegram Memory Moderator

Groq-powered Telegram bot with saved group history, summaries of up to 1,000 messages,
natural-language future moderation rules, durable false-flag memory and manual ban approvals.
Includes rich replies, immediate reply/link actions, media warnings and optional owner-authenticated older history.
Default model: `openai/gpt-oss-120b` at `https://api.groq.com/openai/v1`.

## Run on an Ubuntu / Oracle Linux VPS

Python 3.10 or newer is supported. Ubuntu packages: `python3`, `python3-venv`, `git`.

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
cp .env.example .env
chmod 600 .env
nano .env
python -m bot.main
```

Run these commands from the repository directory. Installing the project itself is unnecessary
when starting it with `python -m bot.main`. If you already installed `requirements.txt` and got a
`build_editable` error from `pip install -e .`, skip that editable-install step. Create/edit `.env`
if needed, then run `python3 -m bot.main` from this directory. Your installed dependencies remain usable.

If startup reports `Only timezones from the pytz library are supported`, an older APScheduler
from another Telegram bot installation is being imported. Run `git pull --ff-only`, reinstall
with `python3 -m pip install --upgrade -r requirements.txt`, then start again. The requirements
pin compatible APScheduler/tzlocal versions. A virtual environment avoids conflicts with other bots.
The bot uses its own persistent asyncio queue and disables PTB's JobQueue after builder initialization.

Fill `BOT_TOKEN`, `GROQ_API_KEY` and `LOG_GROUP_ID`. Set `OWNER_ID` to the numeric ID
of **@yucant** if you know it. Otherwise that username must `/start` in the bot's
private chat once. The bot permanently binds the numeric ID, so a later username
change cannot transfer ownership. Keep the SQLite database when redeploying.

For Docker (recommended for automatic restarts):

```bash
cp .env.example .env
chmod 600 .env
nano .env
docker compose up -d --build
docker compose logs -f --tail=100
```

The named `bot-data` volume preserves messages, rules, ownership, reviews, corrections,
and queued moderation jobs. Do **not** run `docker compose down -v` if you want to keep memory.
One bot process per token/database; do not run several polling replicas.

## Telegram setup

1. Create the bot in **@BotFather** and put its token in `.env`.
2. Disable group privacy with `/setprivacy` → your bot → Disable. Re-add the bot if Telegram requires it.
3. Make the bot an admin in each source group, with **Delete messages** and **Restrict members**.
   Use a **supergroup** for temporary muting. Telegram cannot restrict individual users in basic groups.
4. Add the bot to your separate log group, give it permission to send messages, and set its numeric
   `-100…` ID as `LOG_GROUP_ID`. Put the source-group reviewers in this log group too.
5. As the owner, `/start` privately, then `/enable` inside your source group.
   Collection begins only after enablement. The log group is never monitored or moderated.
6. For a second operator: they `/request` privately or as an admin inside their group; you approve
   the access request in the log group. Alternatively use `/allow NUMERIC_USER_ID` privately.
   Granted users can enable groups where they are current admins. A group request approves both
   the operator and that group. `/deny` revokes that user's rules and groups they enabled.

For messages authored directly by other bots, enable Telegram's **Bot-to-Bot Communication Mode**
in BotFather as well as disabling group privacy and making this bot an admin. Normal inline-bot
results sent by humans are matched using the message's `via_bot` metadata. Bot posts are always
data, never operator instructions, and the bot ignores its own messages.

## Examples

Replace `@YourBot` with the actual bot username. English, Hindi and Hinglish natural instructions
go through the model planner. Deterministic slash commands also work if the model is unavailable.

```text
@YourBot summarize the last 500 messages and give a detailed conclusion
@YourBot summarize the last 1000 messages in Hindi and rate the discussion
/summary 500

# Reply to the promotion example:
@YourBot next time mute users posting promotions like this and request a ban approval
/promo mute_review Unsolicited job promotions asking users to join or DM external accounts

@YourBot delete future promo messages and create review requests in the log group
/promo delete Unsolicited ads with a call to join a channel or contact an external account

@YourBot delete future messages sent via @gif in this group
/inline @gif

# Reply to the user's message:
@YourBot what's written here
@YourBot censor this to delete
@YourBot copy and send me https://t.me/c/2397157264/231866

@YourBot delete upcoming stickers and GIFs from everyone, warn them, kick after three violations

# Future targeting:
@YourBot delete all future messages from this person
@YourBot delete his future stickers
@YourBot delete his future GIFs
@YourBot delete his future videos if they contain @jobs
/target all
/target sticker
/target animation
/target document
/target video @jobs

/rules
/unrule 3
/pending
/memory
/help
```

All installed rules are **prospective**: old messages, including later edits to messages sent before
the instruction, are not deleted. Rules are per group. Explicit targets bind **numeric IDs**, not
changeable usernames. Anonymous admins/channel-authored posts cannot reliably be attributed to a
person and remain protected. Mention filters require the literal `@username` in text/captions;
hidden text links and image content are not treated as mentions.

Automatic promo, inline and group-wide media rules skip current group admins and anonymous/channel senders. The
explicit reply-based target rule is the requested exception: it can delete a targeted admin's
future messages, including their bot commands. It never creates ban requests or mutes admins.

Immediate `censor this to delete` deletes the single replied message and does not install a rule.
The operator must be an approved current group admin. Explicit single-message deletion can include
an admin's message. Telegram restricts deletion by message age/type, usually to messages under 48 hours.
Copying uses Telegram's `copyMessage`, so it can work for an older linked message without importing
history, provided the bot can access it. Protected content and some service/media types cannot be copied.
The source must be an enabled group; copying across groups also requires the operator to be its admin.
`What's written here` reads only the selected text or replied photo, rather than starting a group summary.
Photos use `VISION_MODEL` (default `qwen/qwen3.8-27b`) with the currently configured API/key. Configure a
compatible vision model if you change providers; GPT OSS itself cannot read images. Images are limited to 10 MB.

Group-wide sticker/GIF rules do not need a replied user. They delete the specified media, send warnings,
and keep per-group/rule/user violation counts in SQLite. At the requested limit, the bot removes the member
and immediately unbans them so they may rejoin. This explicit kick rule does not create a permanent ban.
If unbanning fails, the fallback restriction expires in roughly one minute. Admins remain protected.

## Older history and owner authentication

The bot token alone cannot fetch old messages. Add Telegram **application** credentials from
[my.telegram.org/apps](https://my.telegram.org/apps) to `.env`:

```dotenv
TG_API_ID=YOUR_NUMERIC_APPLICATION_ID
TG_API_HASH=YOUR_APPLICATION_HASH
TG_USER_SESSION_PATH=data/history-owner.session
VISION_MODEL=qwen/qwen3.8-27b
```

Restart the bot after changing environment variables. As the permanently bound owner, in the bot's
**private chat**, send `/login +COUNTRYCODEPHONENUMBER`. Telegram sends a login code to your account.
Enter it using the private inline keypad and press **Submit**. The keypad is bound to the owner,
the private chat, a random nonce and its message, and expires after five minutes. Code entry is never
stored in SQLite, echoed, or logged. Do not send/forward the OTP as a text message: Telegram invalidates
login codes sent into another chat. If Telegram requires two-step verification, use `/login2fa PASSWORD`
privately; that message is deleted immediately and is never added to memory. Alternatively use terminal login below.
Only the numeric owner can authenticate, even when other operators have been approved. Logging in as
another account is rejected and that newly created session is revoked.

The reusable account credential is saved in the session file with owner-only filesystem permissions.
`.env` stores the **path**, rather than the session credential; keep the `data` directory private and
do not commit or share session files. Docker persists the session in the existing data volume; `env_file`
provides the path at startup. `/history_status` verifies authentication. `/logout` revokes the reader session.
The reader only reads enabled source groups that the owner already belongs to. It never joins groups,
sends messages as the owner, or applies old moderation rules to imported history.

Once authenticated, `/summary 500` or `/summary 1000` imports the requested recent history before the
request, then summarizes the saved snapshot in chunks. `/history 500` explicitly imports without summarizing
(approved current group admins only). Telegram only returns history visible to the owner's account;
deleted messages and history hidden from that account cannot be recovered. The bot reports the actual
available count rather than inventing missing messages.

Terminal alternative (stop the bot first to avoid simultaneous session access):

```bash
python3 -m bot.history
python3 -m bot.main
```

## Moderation and feedback

Promo rules retrieve relevant saved corrections, classify the new text/caption and nearby context,
then ask a separate evaluator to review the evidence. Both must reach the configured confidence
threshold, and the evidence must actually occur in the message. The same underlying model powers
both calls; this is a second evaluation, not an accuracy guarantee.

A durable review request is sent to the log group **before** any deletion or mute. If delivery fails,
the bot does not apply the punishment. Muting defaults to 60 minutes and does not overwrite an
existing moderator restriction. A rich notice in the source group names the muted member, gives the
reason, and says **Waiting for admin inspection** with the review number and temporary mute duration.
Review buttons:

| Button | Result |
| --- | --- |
| Approve ban | Requires a current admin of the **original group**; rechecks that the target is not an admin; applies ban. |
| Cancel / restore mute | Keeps the message classified but cancels this request; restores only this bot's unchanged mute lease. |
| False flag / learn | Restores the bot-owned mute where possible, saves an exact exception and a narrowly scoped lesson for later retrieval. |

The owner and current original-group admins can cancel or mark false flags, even if an admin has
not been granted operator access. Log-group admin rights alone give no review authority. Bans
always require original-group admin approval, including when the reviewer is the bot owner.
Reviews are claim-once and remain usable after restarting. Buttons are acknowledged before permission
lookups; interrupted claims recover at startup. A failed keyboard edit cannot roll back a completed
decision. `/pending` reposts pending reviews. False flags save an exact exception immediately; the
broader AI lesson runs in the background, so slow model requests cannot hold up mute restoration.

Bot text replies use Telegram's native `sendRichMessage` with escaped headings and bold emphasis.
Set `RICH_MESSAGES=html` for an older self-hosted Bot API server; unsupported native methods fall back
to formatted HTML. Telegram controls fonts and rendering. Copied content retains its original format.

Deleted messages cannot be restored. A false flag prevents the same normalized text being flagged
under the same rule and supplies examples/lessons to future model decisions; similar messages may
still be misclassified. This is **retrieval memory**, not online training of model weights. There is
no guarantee of never making the same kind of mistake. Installing a different rule deliberately
starts a different policy scope; it does not inherit exact exceptions from unrelated policies.

## Models and OpenAI-compatible APIs

Owner-only commands in private chat:

```text
/models
/model openai/gpt-oss-120b
/api https://api.example.com/v1 their-model-id OTHER_API_KEY
/format schema
/format json
/format text
```

Define `OTHER_API_KEY` in the server environment first. The command names the **environment variable**,
not the key value. Restart/recreate the process after adding a new environment variable. Docker:
`docker compose up -d --force-recreate`. The model, endpoint, key variable name and response mode are
saved in SQLite; key values are never written to the database or log group. Public HTTPS endpoints
are supported. `/model` saves the choice; `/models` checks the endpoint's model list. Switching to a
provider that lacks `/models` does not necessarily prevent its chat completions from working.

Default `schema` uses strict JSON schemas supported by Groq GPT OSS. For compatible providers without
strict schemas, select `json` or `text`; outputs are still validated locally. The provider must support
OpenAI-style `/chat/completions`, `max_tokens`, and the chosen response mode. Groq uses
`max_completion_tokens`; GPT OSS uses low reasoning effort and excludes reasoning from replies.
Truncated completions retry with a larger bounded output budget. Partial JSON is never executed.
API/model outages prevent
AI moderation actions and produce an alert; deterministic targeting still works. Rate limits are retried
briefly. Large busy groups can build a queue and incur substantial model usage: every text-bearing
nonadmin message covered by a promo rule needs at least one classifier call, and flagged messages
need a reviewer call. No paid API quota is included.

## Memory, summaries and limitations

- SQLite retains observed text/captions, media labels, author IDs, timestamps, current edits, rule
  definitions, queued jobs, review outcomes and all admin corrections. No automatic expiry.
- Only **enabled** groups are collected; unauthorized private chats and the log group are not archived.
- With owner history login configured, summaries first import accessible older messages. Without it,
  `/summary 1000` means up to 1,000 **saved** messages, excluding the request; 42 available reports 42/1000.
- Saved deleted messages are still part of historical summaries and are marked as deleted where known.
  Deletions by other admins aren't automatically reported to this bot.
- Summaries chunk all selected messages, reduce notes and synthesize decisions, questions, contributions
  and conclusions. Long individual text is truncated at 3,000 characters, with a disclosed count.
  Ratings are subjective assessments of message contributions, never facts about a person's character.
- Group summaries represent media by metadata/captions. Explicit reply-photo reading uses the vision model;
  audio/video transcription and general file parsing are unavailable.
- Telegram queues updates only temporarily (normally up to 24 hours). Long downtime can lose unseen
  messages. Local jobs resume after restart; failed model analysis is skipped with an alert, rather
  than applying an uncertain punishment later.
- Persistent memory depends on keeping the disk/volume and backups. `/backup` sends a consistent
  SQLite backup only to the owner in private chat. To restore, stop the bot, replace its database
  with the backup, remove stale `-wal`/`-shm` sidecars, then restart with the original owner ID.
- Group messages/captions selected for analysis are sent to the configured AI provider. Tell group
  members the bot stores messages and uses that provider. Secure the VPS, `.env`, backups and log group.

## Agent architecture

This project uses a controlled **agentic workflow** suited to moderation: instruction routing and
typed action plans; group-scoped retrieval; classifier/evaluator gates; locally authorized Telegram
actions; durable human-feedback memory; and a restartable queue. The application owns permissions,
rules and execution. The model cannot grant access, change keys, run shell commands, ban autonomously,
or rewrite its own code/policies. Group transcripts are treated as untrusted data in every prompt.

Prompts are in `bot/prompts.py`, typed plans/verdicts in `bot/models.py`, persistence in `bot/db.py`,
provider calls and summaries in `bot/llm.py`, and Telegram orchestration in `bot/service.py`.

Design references:
- [Building effective agents — Anthropic](https://www.anthropic.com/engineering/building-effective-agents)
- [Local tool calling — Groq](https://console.groq.com/docs/tool-use/local-tool-calling)
- [Structured outputs — Groq](https://console.groq.com/docs/structured-outputs)
- [Telegram Bot API](https://core.telegram.org/bots/api)
- [Telegram bot-to-bot communication](https://core.telegram.org/api/bots/bot-to-bot)

## Tests

```bash
python -m pip install --upgrade pip
python -m pip install '.[test]'
python -m ruff check .
python -m pytest -q
```

Tests use fake Telegram responses and an HTTP mock; they need no credentials. They cover authorization,
group isolation, admin protection, prospective rules, inline metadata, media/mention filtering, exact
false-flag suppression, memory persistence, request claims, mute restoration, model/log failures,
provider error redaction and 1,000-message chunk coverage. Live API accuracy and delivery must be
verified in a small test supergroup after supplying your real keys. GitHub Actions runs the checks.

## Create and push a new GitHub repository

If starting from the ZIP and GitHub CLI is installed/authenticated:

```bash
git init -b main
git add .
git commit -m "Build persistent Groq Telegram moderator"
gh repo create telegram-memory-moderator --private --source=. --remote=origin --push
```

`.env`, database files, backups, caches and runtime data are excluded from Git. Never commit tokens.
