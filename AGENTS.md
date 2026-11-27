# Installing the bridge for a user

- Read README.md and docs/architecture.md before executing installation. Use macOS, the official signed Codex bundle, Python 3.14+, and the user's signed-in Droid CLI.
- Keep the checkout in a persistent directory. LaunchAgents reference that path. Run the offline tests before installing.
- Keep Extra Usage off. Let the user authorize any fresh sign-in or Droid folder trust, then confirm all three windows with `setup.py status`. Leave the account's spending settings alone.
- Check for existing services on ports 17382 and 17383. If they belong to another installation, preserve it and inspect its status before proposing a migration.
- Prove real model routing with `tests/live_matrix.py --approvals` in disposable chats. Run `--all` only when the user wants account-wide coverage and accepts the Standard credits it consumes.
- Quit and relaunch the desktop only when active chats have finished. Use the sidebar or the supplied local thread link to reopen the existing conversation.
- Keep credentials, session files, tool output, raw usage receipts, and personal identifiers out of commits and issue reports. Use only synthetic histories and redacted aggregate results in tests and reports.
