# Codex Nightshift

Codex Nightshift explicitly adopts ordinary Codex App or CLI sessions and
resumes unfinished work after a usage-window interruption.

Inspired by [dujunyi416/claude-nightshift](https://github.com/dujunyi416/claude-nightshift).
This is an independent Codex-oriented implementation.

## Graphical control panel

Double-click `codex-nightshift-gui.cmd`, or run:

```powershell
.\codex-nightshift.cmd gui
```

The local dashboard shows recent Codex tasks and lets you choose, per task:

- **Auto resume on:** the background watcher may continue this task.
- **Auto resume off:** the task is never executed by Nightshift.
- **Interrupted or stalled:** resume quota interruptions and idle unfinished turns.
- **Quota exhausted only:** strict mode; ignore ordinary idle unfinished turns.
- **First instruction after resume:** set a task-specific continuation prompt. Leave
  it blank to use the global `resume.prompt` default.

The GUI only listens on `127.0.0.1`. It also provides controls for installing,
starting, and stopping the Windows background watcher.

It is intentionally conservative:

- It only manages sessions you explicitly adopt.
- It never sends warm-up prompts.
- Passive monitoring only reads local JSONL transcripts and consumes no tokens.
- Resumed work runs with the `workspace-write` sandbox by default.
- `--dry-run` shows what would happen without invoking Codex.

Codex Nightshift reads the local session transcripts under `~/.codex/sessions`.
Those transcripts contain the session ID, original working directory, turn
lifecycle events, and the latest 5-hour and weekly usage-window snapshots.

## How interruption detection works

Nightshift uses Codex transcript lifecycle events instead of trying to infer
completion from the assistant's prose:

- A `task_complete` event means the Codex turn ended, not necessarily that the
  user's task was completed. It counts as **completed** only when it contains a
  non-empty final agent answer.
- A `task_complete` event with no final agent answer means **interrupted** with
  high confidence. This is the pattern seen when Codex stops mid-work and the
  user later has to send "continue".
- A `turn_aborted` event after the latest `task_started` means **stopped**. It
  is not resumed automatically.
- A recent unfinished turn is considered **active** while its transcript is
  still changing.
- A started turn with no later completion or abort event becomes **possibly
  interrupted** after the configured idle period.
- If that unfinished turn also has a matching exhausted quota snapshot, the
  interruption is high confidence. Otherwise it is medium confidence.

There is no perfect way to distinguish a crash, lost process, closed app, or
deliberately abandoned unfinished turn when no terminal event was written.
Strict mode therefore resumes only high-confidence quota interruptions. Normal
mode may also resume medium-confidence idle unfinished turns, but only for
tasks you explicitly enabled.

## Install

Python 3.10 or newer and an executable Codex CLI are required. On Windows,
Nightshift automatically checks the Codex App-managed CLI under
`%LOCALAPPDATA%\OpenAI\Codex\bin` before falling back to `PATH`.

```powershell
# Install Codex Nightshift from this directory.
pip install -e .
codex-nightshift doctor
```

If `doctor` cannot launch Codex, install the standalone CLI or create the config
and set `codex_cmd` to the full path of an executable Codex CLI:

```powershell
codex-nightshift config
```

## Typical workflow

On Windows, commands can be run directly from this source directory without
installing the package:

```powershell
.\codex-nightshift.cmd doctor
```

List recent ordinary sessions:

```powershell
.\codex-nightshift.cmd sessions --verbose
```

Adopt the most recent session:

```powershell
.\codex-nightshift.cmd adopt --last
```

Preview watcher behavior:

```powershell
.\codex-nightshift.cmd watch --dry-run --once
```

Start the watcher:

```powershell
.\codex-nightshift.cmd watch
```

Install the watcher as a Windows logon background task and start it now:

```powershell
.\codex-nightshift.cmd background install --start-now
.\codex-nightshift.cmd background status
```

The background task does not adopt sessions by itself. It only watches sessions
you explicitly adopted. Stop or remove it with:

```powershell
.\codex-nightshift.cmd background stop
.\codex-nightshift.cmd background remove
```

The scheduled task points at the current source directory. If you move the
directory later, run `background install` again from the new location.

When an adopted session ends with an unfinished turn, Nightshift waits until
any exhausted usage window resets, then invokes:

```text
codex exec --json --sandbox workspace-write --skip-git-repo-check resume <SESSION_ID> <CONTINUE_PROMPT>
```

Use strict mode if you only want automatic resume when the transcript contains
an exhausted usage-window snapshot:

```powershell
.\codex-nightshift.cmd adopt --last --strict
```

Stop managing a session:

```powershell
.\codex-nightshift.cmd unadopt <SESSION_ID_OR_PREFIX>
```

## Commands

| Command | Purpose |
| --- | --- |
| `status` | Show the latest usage windows and adopted session states |
| `sessions` | List recent ordinary Codex App/CLI sessions |
| `adopt` | Allow Nightshift to resume one session |
| `unadopt` | Stop managing a session |
| `scan` | Show adopted sessions that currently look interrupted |
| `resume` | Resume now or process adopted sessions once |
| `watch` | Continuously monitor and resume adopted sessions |
| `background` | Install, start, stop, inspect, or remove the Windows watcher task |
| `gui` | Open the graphical task selection panel |
| `config` | Create and show the JSON configuration |
| `doctor` | Verify the standalone Codex CLI |

## Safety and limitations

An idle unfinished turn is not always caused by a usage limit. It may also mean
the App was closed, the machine restarted, or the user deliberately stopped a
turn. Explicit adoption is the consent boundary. Use `--strict` when false
positives are unacceptable.

Codex resumes at the conversation level, not from the exact interrupted token.
The continuation prompt asks Codex to inspect current repository state before
finishing the remaining work.

The transcript format is an internal local format and may change in future
Codex releases. Run `codex-nightshift scan` and a dry run after upgrades.
