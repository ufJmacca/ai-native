You are resolving a dependency merge conflict before a slice continues.

The repository is currently in a conflicted git merge state after applying a dependency commit to the active slice worktree. Resolve only the merge conflict. Preserve the dependency behavior, preserve the active slice intent, and avoid unrelated implementation work.

After resolving the files, run the most relevant lightweight checks you can. Do not commit; ai-native will complete the merge commit after you return.

Feature spec:
{spec_text}

Active slice:
{slice_definition}

Dependency:
- Slice: {dependency_id}
- Commit: {dependency_commit}

Conflicted files:
{conflicted_files}

Git status before repair:
{git_status}

Run directory:
{run_dir}

Return a concise markdown summary of what conflicts you resolved and what checks you ran.
