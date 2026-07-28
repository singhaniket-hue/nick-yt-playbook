# Synthetic demo project

This fictional, deliberately tiny project demonstrates the script-marker and
research-catalogue shapes without carrying any production episode material.
The script is not intended to pass RabbitHole's 30–40 minute validation gates.

Generate three four-second color clips and a silent narration WAV locally:

```text
uv run python scripts/create_demo_media.py
uv run rabbithole resolve prepare examples/demo-project
```

The generated files are ignored by Git. The command performs no network
access and never opens Resolve. Copy the shapes into a Git-ignored
`projects/<slug>/` workspace when starting a real episode.
