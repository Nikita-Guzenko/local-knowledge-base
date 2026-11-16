# Contributing

Keep the core usable with Python 3.10+ and the standard library. Use synthetic fixtures; never commit real sessions, exports, databases, credentials, or private paths.

For a connector, document required configuration and source format. Keep ingestion repeatable, preserve original pointers, and return a nonzero exit status when input cannot be read. Add integration coverage against a temporary SQLite database.

Before opening a pull request, run:

```bash
python3 -m unittest discover -s tests -v
```

Describe the behavior changed and include the command and result under “How tested.” For new source formats, include a small synthetic example and any format limitations.
