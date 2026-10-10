# leia-sync

Mirrors your courses from LeIA (`leia.pw.edu.pl`, PW's Moodle) into local folders:
one folder per semester, one per course, plus a timeline of deadlines and a changelog.
It only reads. It never submits an assignment, starts a test or joins a meeting.

## Setup

### 1. Config

```sh
cp config.example.toml config.toml
```

Edit `config.toml` (it is gitignored):

- `username`: your USOS login, the one you type on `cas.usos.pw.edu.pl`.
- `data_dir`: where the semester folders should go. Defaults to the folder above this repo.

### 2. Password

LeIA logs you in through USOS CAS, so the script needs your USOS password. It is never
written to a file in this repo.

**macOS**: store it in the Keychain. The command prompts for the password, so it does not
end up in your shell history:

```sh
security add-generic-password -s leia-sync -a <your-usos-login> -w
```

`-s` must match `keychain_service` in `config.toml`, and `-a` must match `username`.
To change the password later, add `-U` to the same command. To remove it:

```sh
security delete-generic-password -s leia-sync
```

The first time the script reads the entry, macOS may ask whether to allow access.
Choose "Always Allow", otherwise the scheduled run will hang on that prompt.

**Linux / Windows**: set the `LEIA_PASSWORD` environment variable for the process that
runs the script. It is used whenever no Keychain entry is found.

### 3. First run

```sh
uv run leia_sync.py --selftest   # checks the safety rules, no network
uv run leia_sync.py              # full sync; prints what changed
```

[uv](https://docs.astral.sh/uv/) installs the Python dependencies on its own. The first run
downloads every file (lecture videos included), later runs only fetch what changed.

### 4. Daily schedule

The scheduled job is a Claude Code agent that runs the script, summarises what changed and
what is due, and can repair the script if LeIA's layout changes. Its instructions are in
`scheduled-task-prompt.md`.

**macOS, launchd** (needs the `claude` CLI, logged in):

```sh
./install-schedule.sh          # every day at 07:40
./install-schedule.sh 18 15    # or pick hour and minute
./install-schedule.sh remove
```

macOS blocks background jobs from reading `~/Documents`, `~/Desktop` and `~/Downloads`
("Operation not permitted" in `last-run.md`). If the repo or `data_dir` is in one of those,
either keep them elsewhere (for example `~/University`), or allow `/bin/sh` under
System Settings > Privacy & Security > Files and Folders (or Full Disk Access), or use the
desktop app option below.

The report of the last run is written to `last-run.md` in this repo. The Mac must be awake
and you must be logged in; a missed run happens on next wake.

**Claude desktop app**: create a scheduled task there, set its folder to this repo and paste
the contents of `scheduled-task-prompt.md` as the prompt.

**No agent**: put `uv run leia_sync.py` in cron. You lose the summary and the self-repair.

Cloud-hosted schedules do not work: they cannot reach your Keychain.

## What you get

```
<data_dir>/
  CLAUDE.md          guidance for AI agents working with the data (generated)
  TIMELINE.md        every deadline, test window and meeting, upcoming first
  CHANGELOG.md       what each sync found new, changed or removed
  NOTIFICATIONS.md   LeIA notifications with full text
  semestr-1/
    <course>/
      leia/          mirror of the course. Overwritten on every sync, do not edit.
        README.md    index of all materials
        tasks.md     assignments and tests with deadline and status
        grades.md
        sections/NN-<section>/MM-<activity>.md   plus a same-named folder with its files
      work/          yours: solutions/, research/, notes.md. Never touched by the sync.
```

Semesters come from the term code in the course name (`...#2026Z`, `...#2027L`): your earliest
term is `semestr-1`. Courses without a code go to `semestr-1`. If you start using this in a
later semester, set `first_term = "2025Z"` (your real first term) in `config.toml`.
A course keeps its folder even if it is renamed on LeIA.

## Limits

- Test questions are not mirrored: LeIA only shows them inside an attempt, and the script never
  starts one. Reviews of attempts you finished yourself are mirrored.
- Fetching a page counts as viewing it: "view" completion marks get ticked and forum posts
  become read. Notifications stay unread.
- Meeting recordings (BigBlueButton) and external links are listed, not downloaded.
- Booking activities (`scheduler`) are not opened.
- If USOS adds a second login step, the scripted login will fail with a clear message.

## Maintaining it

- `docs/platform.md`: how LeIA is built, which endpoints work, what must never be requested.
- `templates/CLAUDE.md`: source of the generated `CLAUDE.md`.
