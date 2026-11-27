# Architecture and data flow

```text
Signed Codex desktop backend
  | local Responses and catalog requests
  v
127.0.0.1:17383
  |-- GPT catalog, authentication and GPT turns --> chatgpt.com
  '-- Factory model and readable context --> 127.0.0.1:17382 --> signed-in Droid CLI
                                             ^                   |
                                             '-- Codex tool relay'
```

The launcher uses `CODEX_CLI_PATH` to run the bundled signed executable through `signed-codex`. The wrapper supplies `openai_base_url` to the app server for this launch. The app server remains responsible for loading, saving and changing models in the chat. Native WebSocket requests fall back to HTTP event streams through the bridge.

`bridge.py` handles the merged catalog, signed-in Codex session verification and provider routing. The two services bind to IPv4 loopback and check Host and Origin. The OpenAI bearer token goes to a fixed OpenAI destination. The local Factory gateway uses a randomly generated file token with owner-only permissions. Each Droid run gets its own temporary, unguessable MCP capability. Tool results are matched to outstanding calls and their Codex chat. Codex still evaluates commands, file changes and permission requests.

`gateway.py` invokes Droid with only the Codex relay and Droid's associated tool helpers available. Its local receipt records time, model, effort, Factory credits and tool-call count, without storing chat text in the receipt. Droid itself can store its per-turn session transcript locally. The bridge disables cloud session sync for these runs, and Factory receives the readable context needed to serve the turn. Use Factory and your selected model only for work you are authorized to send to them.

## Scope and known limits

- The catalog shows enabled, exact Factory-managed model IDs that Droid reports to the account. Auto routing, disabled models and custom or bring-your-own-key models are excluded from the exact-model Standard path. Availability can change between discovery and a request. An attribution mismatch fails the turn.
- Text and supported Codex function and freeform tools cross the bridge. Readable tool results from prior compacted turns are replayed where available. Current image, audio and file inputs are outside the text transport. Earlier media appears as an omission marker when its message is available. Encrypted model state and private reasoning have no cross-provider representation.
- GPT compaction can be reconstructed from its local checkpoint when the corresponding journal is readable. Archive replay is capped at 96 KiB, and ambiguous, rolled-back or oversized history fails explicitly. Factory-side compaction and OpenAI-hosted web search are outside this adapter. Cloud-only chats and every Codex connector are outside the verified scope.
- The quota page is a separate local view of Factory usage. The native OpenAI account meter remains OpenAI's. No additional usage starts without trustworthy Standard readings and a zero Extra Usage balance.
- The integration relies on the current shipping app-server catalog and Droid CLI interfaces. A vendor update may require changes.

## Troubleshooting

- GPT appears alone. Verify you launched via `setup.py launch`, the desktop is signed in, Droid is signed in, and `setup.py status` shows the refreshed Factory catalog after opening the picker.
- Limits are unavailable. Run `droid` once from the empty `droid` subdirectory in the installer's local state directory and check `/limits`. Verify all three windows and resolve any sign-in or folder trust prompts in Droid.
- The chat is open in another app. Close the other owner of the local thread, let it save and retry the original chat. Keep the existing thread ID.
- A tool is missing. Check whether that tool is available in the same Codex workspace on GPT, and check the model's disposable-chat receipt. Tool permission behavior follows the existing Codex policy.
- Installation reports a port conflict. Inspect the local service that owns the port and decide whether to migrate it. The installer stops at conflicts and leaves the existing services alone.
