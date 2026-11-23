# Guides

| Guide | For |
| --- | --- |
| [Administrator Guide](admin-guide.md) | Deploying and configuring the harness, and adding systems, profiles and scenarios |
| [User Guide](user-guide.md) | Connector developers and testers: pointing a connector at a system, moving the clock, scoring answers |

The Markdown files are the source. CI builds a Word copy of each (the `guides` artifact of the `guides` job); to build them locally:

```bash
pip install python-docx   # and pandoc 3.x
python scripts/guides/build_docx.py      # writes .build/guides/*.docx
```

Sections marked "To write" are still skeleton.
