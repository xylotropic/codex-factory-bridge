# Verification

The first macOS release was exercised with Codex 0.171.0-alpha.13.2 and Droid 1.21.1. Isolated live chats were driven through the original signed Codex app server with both accounts authenticated. Test prompts, files, and tool results were synthetic.

- The Codex model catalog and the signed-in Droid catalog agreed on 79 eligible Factory model IDs on the test account.
- Claude Opus 5.5 Max and Claude Fable 5.2 Max were exercised in one chat after GPT. Both remembered an exact earlier marker. Codex accepted one real shell approval and declined another. The accepted command created an empty scratch file, the denied command did not, and the same chat returned to GPT.
- Beam 1.0, Claude Fable 5.1, Factory GPT-6.2 Sol and Gemini 3.5 Flash also passed live checks for earlier context, shell output and exact model receipts.
- Three Standard windows were read at each live test boundary and Extra Usage remained off. The quota dashboard is separate from the Codex account meter.
- The macOS installer loaded both user services, returned ready and all three quota windows, then unloaded both services in an isolated test. The original signed binary passed the macOS signature check.
- The offline tests cover transport boundaries, cross-thread tool continuations, duplicate requests, and replay of readable archived chat and tool results. Use the live test in README.md to qualify the models and tools your account actually offers.

The full matrix is account-dependent. A successful test for one model and tool does not guarantee every model, input type, connector or future app version.
