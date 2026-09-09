# After install

1. Enable the Python plugin: `hermes plugins enable argos`.
2. Restart the Hermes gateway/Desktop backend so the CLI and REST router reload.
3. In Desktop Settings → Plugins, enable **ARGOS**, then run **Reload desktop plugins** from the command palette if it does not appear immediately.
4. Add each account with `hermes auth add openai-codex`.
5. Verify with `hermes subs doctor` and `hermes subs status`.
