Before every response, always (always always, without fail) read the contents of the directory `context` and all of its subdirectories. Use the information in that context directory (and all of its files) to inform your responses.

When writing files, only write them to the 'pai_workspace/output' directory so it's easier to manage and clean.

When installing Python dependencies, always use `uv` instead of `pip`.

Never commit or push. Those are tasks only the user does. You may run read-only git commands (e.g. `git status`, `git diff`, `git log`), but never run `git commit`, `git push`, or any other command that creates commits or writes to a remote.
