# 0006 — Bismuth owns its configuration; the app is the settings UI

**Status:** accepted
**Supersedes:** the environment-variable configuration described in the first draft of [0004](0004-llm-provider-abstraction.md)

## Context

This one was learned the hard way, in public, and the story is the argument.

A user put a working `OPENAI_API_KEY` in their `.env`. Every call failed:
*"Your account is not active, please check your billing details."* We read that
error and concluded their OpenAI account had a billing problem. It did not. The
key in `.env` was never used — a *different*, long-dead `OPENAI_API_KEY` had been
set in their Windows user environment at some point and forgotten, and our code
preferred it, on the principle that "an exported variable is deliberate; a file is
a default."

The principle is defensible. The outcome was a tool that ignored the setting the
user had just typed, used a credential they had no memory of, and reported the
result as somebody else's billing problem. Nothing anywhere pointed at the cause.

What made it worse was the shape of the fixing. Each symptom got a patch:

1. `.env` wasn't reaching provider SDKs → add `load_dotenv()`.
2. Which file wins? → argue about `override`.
3. `import litellm` scavenges a `.env` from above the virtualenv and wins the race
   → make the import lazy.
4. A present key proves nothing → add `doctor --probe`.

Four patches, each correct, none addressing the actual problem — which the user
named before we did: **a tool that assembles its configuration out of ambient
state cannot tell anyone what it is doing.** Every patch was another guess about
whose environment variable should win, and the honest answer is that none of them
should.

There was a second complaint in the same breath and it was the same complaint:
fifteen `BISMUTH_*` variables, a hand-typed `openai/gpt-5.4-nano` string, and a
five-command startup sequence, in front of a tool whose entire pitch is that it
saves you work.

## Decision

**Bismuth owns its configuration. It never goes looking.**

### One key, read from one place

Bismuth reads exactly one credential: its own `Settings.api_key`. It does not read
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, or any other provider variable, ever. And it
passes the key to LiteLLM as an **explicit argument** rather than leaving it in
`os.environ` to be discovered:

```python
kwargs["api_key"] = self._api_key      # adapters/llm/litellm_adapter.py
```

Whatever the OS has lying around is now irrelevant *by construction*. The bug is
not fixed; it is unrepresentable.

The `BISMUTH_` prefix does the rest of the work: nothing else on any machine is
called `BISMUTH_API_KEY`, so the collision cannot recur.

### The app is the settings UI

`bismuth` — no arguments — starts the server and opens a browser. If nothing is
configured, that page is the setup screen: folder, provider, key, models.

We had refused this on the grounds that "a key typed into a web form is a key we'd
have to store." That argument was wrong, and worth recording as wrong: the key was
already being stored, in plaintext, in `.env`. A settings page adds no exposure
whatsoever — it only moves who types it where. It was fastidiousness dressed as
security, and it cost users a manual.

### Ask the provider, don't make people type

Users no longer type model names. The wizard asks the provider what this key can
reach (`adapters/llm/catalog.py`) and shows a dropdown.

This also replaces credential checking outright. **Listing the catalogue *is* the
check**: a provider that hands back models is a provider that will answer. The old
`doctor` verified that a key *existed* and called it a pass — which is how a dead
key looked healthy right up until the first document.

### Precedence: short enough to say out loud

```
explicit argument  >  BISMUTH_* env  >  ./.env  >  ~/.bismuth/config.json  >  default
```

The wizard's file sits at the bottom deliberately: a Docker deployment sets
`BISMUTH_*` and must beat whatever is in the image's home directory.

### Config lives in `~/.bismuth/config.json`, not the vault

Per-user, not per-vault, because a vault gets synced to Dropbox and mailed to
colleagues and an API key must not travel with it. Which models you run is a fact
about your machine, not about your documents. Written 0600, atomically, chmod
before content.

### Fifteen environment variables became zero required

The tuning knobs kept their defaults and left the documentation's front page. They
are still settable for people with a reason; they are no longer a wall in front of
people without one. `save_user_config` writes only what a human actually chose, so
upgrading Bismuth improves the defaults instead of being pinned to a snapshot of
setup day.

## Consequences

- The failure that started this is impossible. A test asserts that an ambient
  `OPENAI_API_KEY` has no effect at all.
- Setup is `pip install` then `bismuth`. No file to edit, no model name to look up,
  no prefix to get wrong.
- A user with `OPENAI_API_KEY` already exported must paste it once. That is the
  price of determinism and it is worth paying. (The wizard could offer to import a
  detected key — with explicit consent, never silent precedence — and that would be
  a fine addition.)
- The provider list (`PROVIDERS`) is now a thing to maintain. It is an on-ramp, not
  a wall: `custom` reaches anything speaking the OpenAI protocol, and a fully
  qualified model name passes through untouched.
- Model listing is per-provider HTTP we own, not LiteLLM. LiteLLM unifies
  *inference* — the hard part, and why we use it. One GET per provider is not worth
  routing through a library that would have to guess our credentials.

## What this cost, and the wider lesson

Four patches and a user having to tell us twice. The tell was there the whole time:
when the fixes for a bug keep needing fixes, the bug is not where you are looking.

The related lesson is in `tests/test_litellm_adapter.py`, which exists because a
real run — not the 113 passing tests — found that `usage.retries = attempt` raised
on a frozen model, i.e. the adapter had never once worked. Everything was tested
against `FakeLLM`, which is right for the placement rules and left the real adapter
with no test at all. **A layer whose whole job is talking to the outside world does
not get to be tested only through a stub.**

## Revisit when

Someone needs OS-keyring storage. The `keyring` library is the obvious answer and
was rejected only for install friction (headless Linux, Docker) — real, but a
reasonable thing to add as an optional backend behind the same `Settings.api_key`
seam this ADR establishes.
