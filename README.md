# Codex Factory Bridge

Use models from your signed-in Factory Standard plan in the macOS Codex Desktop model picker. Choose a Factory model in an existing local chat and switch back to GPT from the same picker.

This is an independent, experimental integration. It runs two local services and launches the original signed Codex executable with a per-process endpoint setting. Factory handles model inference through your local Droid CLI. Codex owns the chat, tools, workspace and approvals. The reference account passed live context and tool checks on 78 Factory models. [Model matrix](docs/model-matrix.md)

## Install on macOS

You need the official ChatGPT desktop app with Codex and a working ChatGPT sign-in, a Factory Individual plan with Standard allowance and a signed-in [Droid CLI](https://docs.factory.com/droid-exec/overview), and Python 3.14 or newer. The current reference versions are Codex 0.171.0-alpha.13.2 and Droid 1.21.1.

Keep the checkout in a persistent folder. If you use Homebrew, `brew install python@3.14` installs the required Python.

```sh
git clone https://github.com/xylotropic/codex-factory-bridge.git
cd codex-factory-bridge
python3.14 setup.py install
```

On first install, open Droid in the empty local directory below. Review the path, accept Droid's folder trust prompt for that directory, and run `/limits`. Select **Switch to Droid Core** for exhausted Standard usage and check that Extra Usage is off and its balance reads **$0.00**. Exit Droid after confirming the three windows. The bridge refuses Factory turns when it cannot verify these settings or a Standard window is exhausted.

```sh
cd "$HOME/Library/Application Support/Codex Factory Bridge/droid"
droid
```

Return to the checkout, quit ChatGPT Desktop, then run `python3.14 setup.py launch`. Open your local chat from the sidebar or pass a saved `codex://threads/…` app link with `--thread`. Choose a model ending in **· Factory**. Opus 5.5 and Fable 5.2 default to Max when the account offers it. The model list reflects your account and can change.

The launcher adds the Factory catalog to local Codex chats on this Mac that use the OpenAI provider. Regular ChatGPT chats, cloud Codex chats, and chats on another machine use their own model catalogs. A model choice applies to a new turn. Messages added while a turn is running can steer the model that already started it.

When Codex exposes its subagent tools in a local chat, a Factory parent can ask Codex to start a native GPT child. Pick Fable 5.2 and send “Use one GPT-6.2 Sol subagent with a fresh context for this review. Wait for its result.” The parent uses Factory Standard and the native child uses the OpenAI route.

Open the [local quota dashboard](http://127.0.0.1:17382/) or run `python3.14 setup.py status` for the independent 5-hour, 7-day and 30-day Standard windows and reset times.

### Give it to an agent

> Set up the macOS Codex Factory Bridge from https://github.com/xylotropic/codex-factory-bridge. Read its README and AGENTS.md first and review the source before installing. Use my existing Droid and ChatGPT sign-ins. Keep Extra Usage off. Ask me to handle first-time sign-in or folder trust if prompted. Verify the three quota windows, then run the Opus and Fable smoke test in disposable chats. Wait for my active chats to finish before restarting the desktop.

## Verify

```sh
python3.14 -m unittest discover -s tests -p 'test_*.py'
```

The live test uses the signed Codex app server, real Factory credits and synthetic disposable chats. It requires a Codex file sign-in available in `$CODEX_HOME/auth.json` or `~/.codex/auth.json`. Set `FACTORY_LIMITS_DIR` to the previously trusted directory above so the temporary gateway can read Droid's limits. Routine verification uses two Factory models. Use `--all` only when intentionally spending the Standard allowance to test the entire eligible catalog.

```sh
export FACTORY_LIMITS_DIR="$HOME/Library/Application Support/Codex Factory Bridge/droid"
python3.14 tests/live_matrix.py --approvals
python3.14 tests/live_matrix.py --features
python3.14 tests/live_matrix.py --subagents claude-fable-5.2
python3.14 tests/live_matrix.py --all
```

The matrix checks that the picker and the Factory catalog agree. For each tested model it requires exact recall from an earlier turn, a real Codex shell call, a correct result that only the shell could retrieve, and a Factory receipt specifying the chosen model and effort. The approval test approves one harmless command and denies another, checks both filesystem outcomes, and switches back to GPT on the same chat. See [verification notes](docs/verification.md) for the published test results.

## How it works

1. The loopback Responses bridge merges exact eligible Factory model IDs into the stock model catalog. It forwards GPT requests to the configured OpenAI service and routes Factory selections to the separate local gateway.
2. The gateway discovers account availability through Droid's local JSON-RPC transport and checks all three limits before each new Factory turn. It restricts Droid to the selected model and the Codex tool relay, then verifies model attribution and usage in Droid's local result.
3. Chat text, readable tool history, and current instructions go to Factory when you choose a Factory model. Codex runs relayed function and freeform tools and decides permissions under the chat's existing policy. The relay can ask Codex to discover deferred tools in configurations where Codex offers that tool.
4. The bridge rebuilds readable context from local journals when a GPT compaction checkpoint would otherwise hide it. Native provider requests still use their native path. OpenAI authorization goes to the fixed OpenAI upstream and the gateway uses its own local token.

See [architecture and data flow](docs/architecture.md) for the request paths, known limits, and troubleshooting.

### Remove it

Quit the bridge-launched desktop, run `python3.14 setup.py uninstall`, and open ChatGPT from Applications. Codex chats remain in their original store. The uninstall prints the path to the preserved local quota receipts and service logs. Setup uses LaunchAgents in your macOS user account and does not write shared Codex configuration.

## References

- [Factory's headless Droid CLI](https://docs.factory.com/droid-exec/overview)
- [Factory's OpenAI-compatible proxy example](https://docs.factory.com/sdk/openai-compatible-proxy)
- [Factory Standard limits](https://docs.factory.com/pricing/individuals)
- [ChatGPT Web for Codex routing pattern](https://github.com/miuuyy/codex-chatgpt-web/tree/v3.0.3)
- [Codex app-server protocol](https://github.com/openai/codex/tree/main/codex-rs/app-server)
