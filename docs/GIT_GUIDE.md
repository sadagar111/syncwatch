# Learning Git with SyncWatch

Each lesson matches a step of the project. Commands go into the **VS Code
terminal** (menu *Terminal → New Terminal*, or Ctrl+\`). Everything shown in
`code style` is typed exactly as written, except the parts in `<angle brackets>`,
which you replace.

**Before typing any command, check two things:**
- **The terminal is PowerShell.** The prompt starts with `PS`, e.g. `PS D:\Projects\syncwatch>`.
  If it ends in `$`, it's Git Bash, where `tools\release.bat` doesn't work (Bash eats the `\`). Switch with the
  **˅** next to the **+** in the terminal panel → **PowerShell**.
- **You're in the repo folder.** Open it with *File → Open Folder* before opening the terminal, and
  `git status` answers *"On branch …"*. *"not a git repository"* means you're in another folder.

The repo is whichever folder holds the hidden `.git` folder. Its name and drive don't matter, and you can
move it like any folder (close VS Code first).

**Words you'll meet**
- **Repository (repo):** a project folder plus its full history. There's one copy on GitHub and one on your PC.
- **Commit:** a saved snapshot of the project with a short message saying what changed. Commits are the history you can go back to.
- **Push / pull:** *push* sends your new commits to GitHub; *pull* brings new commits from GitHub to your PC.
- **Branch:** a separate line of work. `main` is the real version; changes are made on side branches first.
- **Pull request (PR):** a proposal to bring a branch into `main`. It's where you read the changes, see test results and click Merge.

---

## Lesson 1: your first commit (version 0.2.1)

**Goal:** put SyncWatch 0.2.1 on GitHub as the first snapshot of the project.

### 1. Log in once with the GitHub command-line tool
```
gh auth login
```
Answer: **GitHub.com** → **HTTPS** → *Authenticate Git with your GitHub credentials?* **Yes** → **Login with a web browser**. Copy the one-time code it shows, then press Enter and paste the code in the browser.

*Why:* this lets `git` and `gh` talk to your GitHub account without you typing a password each time.

### 2. Tell Git who you are (once per PC)
Find your private GitHub email: GitHub → Settings → **Emails**, under *"Keep my email addresses private"*. It looks like `12345678+name@users.noreply.github.com`.
```
git config --global user.name "<your name or nickname>"
git config --global user.email "<your noreply address>"
git config --global init.defaultBranch main
```
*Why:* every commit records an author. The noreply address keeps your real email out of a public repo.

### 3. Download ("clone") your empty repo
Pick a folder for your projects, e.g. `C:\Projects`. In the terminal:
```
cd C:\Projects
gh repo clone <owner>/<name>
cd <name>
```
This creates `C:\Projects\<name>`, which is linked to GitHub. Open it in VS Code with *File → Open Folder*.

### 4. Copy SyncWatch 0.2.1 into it
Unzip SyncWatch-0.2.1.zip and copy **everything inside its SyncWatch folder** into the repo folder: all folders (`syncwatch`, `assets`, `docs`, `tests`, `tools`, `movies`) and all files (`SyncWatch.bat`, `README.md`, `requirements.txt`, `.gitignore`…). Easiest: open the SyncWatch folder, press Ctrl+A, copy, paste into the repo folder. `.gitignore` starts with a dot and may be hidden in Explorer, so make sure it comes along.

Don't copy films. `.gitignore` already tells Git to ignore the contents of `movies`.

### 5. Look before you commit
```
git status
```
Everything shows in red as *untracked*: Git sees the files but isn't saving them yet. The **Source Control** panel in VS Code (Ctrl+Shift+G) shows the same list.

### 6. Stage and commit
```
git add .
git status
```
The files turn green: they're *staged*, which means chosen for the next snapshot.

On Windows, `git add` may print a warning for many files: *"LF will be replaced by CRLF the next time Git touches it"*. That's harmless: Windows and Linux end lines in text files differently, and Git converts them for you. The `.gitattributes` file (added in 0.3.0) sets these rules exactly.
```
git commit -m "SyncWatch 0.2.1"
```
*Why two steps:* staging lets you choose exactly what goes into a commit. Later you'll often commit only some of your changed files.

### 7. Push to GitHub
```
git push -u origin main
```
`-u` remembers that your `main` belongs with GitHub's `main`, so next time a plain `git push` is enough.

### 8. Check
Open your repo on github.com. You should see the files, the README shown below them, and *1 commit*. Click **1 commit** to see the history.

**Then connect GitHub to Claude** (in claude.ai settings) so I can open pull requests on your repo, and **tell me it's pushed**, with the repo name as `owner/name`. Lesson 2 starts with my first pull request (version 0.3.0).

### If something goes wrong
| Message | What it means / what to do |
|---|---|
| `'git' is not recognized` | Git isn't installed, or VS Code was open during the install. Restart VS Code. |
| `Author identity unknown` | Step 2 was skipped. Do it, then repeat the commit. |
| `push declined due to email privacy restrictions` | Your commit used your real email. Do step 2 with the noreply address, then run `git commit --amend --reset-author --no-edit` and push again. |
| `src refspec main does not match any` | Nothing has been committed yet. Do step 6 first. |
| Anything else | Copy the whole message and send it to me. |

---

## Lesson 2: reviewing and merging a pull request (version 0.3.0)

**Goal:** bring my changes into `main` the way teams do: read them, check the tests, merge.

### 1. Find it
On GitHub, open your repo → **Pull requests** → *"SyncWatch 0.3.0: updates, portable install, automatic tests"*. In VS Code, the **GitHub** icon in the left bar (from the GitHub Pull Requests extension) lists it too. Sign in when it asks.

### 2. Read it
- **Conversation** tab: my description of what changed and why.
- **Files changed** tab: every change, removed lines in red and added lines in green. You don't need to understand every line. Skim, and click a line's **+** to ask a question about it, like a teammate would.
- **Checks** (bottom of the Conversation tab): GitHub's machines run all the tests on Windows and Linux. Wait for the green ✓ marks (about 10–15 minutes). A red ✕ means something failed: click **Details** to see what. I'll fix it in the same pull request.

*Why:* nothing reaches `main` without a person reading it and the tests passing. That's also what keeps your signed releases trustworthy.

### 3. Merge
Click **Squash and merge** → **Confirm**. (If the green button says *Merge pull request*, open the **˅** next to it and pick *Squash and merge*.) "Squash" turns all my small commits into one tidy commit on `main`, so the history reads one step at a time. Then click **Delete branch**: the branch has done its job.

### 4. Bring it to your PC
```
git switch main
git pull
```
`git switch main` makes sure you're on main; `git pull` downloads the merged commit. Your folder now has version 0.3.0.

---

## Lesson 3: protect `main`, then make your own first change

### 1. Protect `main` (once)
GitHub → your repo → **Settings** → **Rules** → **Rulesets** → **New ruleset** → **New branch ruleset**:
- Name: `protect main`, Enforcement: **Active**
- Target branches: **Add target** → **Include default branch**
- Tick **Require a pull request before merging**
- Tick **Require status checks to pass** → **Add checks** → add `Tests (windows-latest)`, `Tests (ubuntu-latest)` and `Portable Windows runtime`
- Tick **Block force pushes**
- **Create**

*Why:* from now on nobody, not you and not me, can change `main` without a pull request whose tests pass. A mistake on some branch can't break the real version.

### 2. Your own change on a branch
Your first change: set where updates come from and create your signing key.
```
git switch -c setup-signing
tools\release.bat set-repo <owner>/<name>
tools\release.bat keygen
```
`keygen` shows **24 words: write them on paper, in order.** It then asks for 3 of them back, and then for a passphrase. Next:
```
tools\release.bat check-backup
```
Type your 24 words from the paper (the first 4 letters of each are enough). It must say *"Your 24 words are correct"*. Store the paper somewhere safe; no photos, no cloud.

*Why a branch:* `git switch -c setup-signing` creates a new branch and moves you onto it. Your work happens there, away from `main`.

### 3. Commit, push, open a pull request
```
git status
git add syncwatch/update/keys.py syncwatch/update/source.py
git commit -m "Add my signing key and the update repository"
git push -u origin setup-signing
gh pr create --fill
```
`git status` should show exactly those two files changed. Your **public** key is safe to publish; the private key stays in `%APPDATA%\SyncWatch\release`. `gh pr create --fill` opens a pull request using your commit message. Wait for the checks, then **Squash and merge** as in lesson 2, and:
```
git switch main
git pull
git branch -d setup-signing
```
The last line deletes your local copy of the finished branch.

---

## Lesson 4: your first release

### 1. Dry run
```
tools\release.bat publish --dry-run
```
This checks everything, runs the tests, builds and signs, but publishes nothing. The first time, it also builds the portable runtime: it downloads Python from python.org (~11 MB) and the libraries (~125 MB), and takes a few minutes. That's cached afterwards. Look into the `dist\v0.3.0` folder: it holds exactly what would be uploaded. `dist` is in `.gitignore`, so it never gets committed.

### 2. Publish
```
tools\release.bat publish
```
It asks for your passphrase, shows what changed, and asks you to type the version number to confirm. Then it tags the commit (`v0.3.0`), uploads everything to a GitHub **release**, and downloads the manifest back to check the signature.

*Tags:* a tag is a permanent name for one commit. `v0.3.0` will always mean exactly this code.

### 3. Try it like a friend would
On the release page (repo → **Releases**), download `SyncWatch-0.3.0-windows.zip`, unzip it somewhere like `Documents\SyncWatch-test`, and start `SyncWatch.bat`. That's the installed version, with its own Python. Your Git folder stays your development copy.

Then follow TESTING.md: I'll send a small test update (0.3.1-test.1) as the next pull request, so you can see the whole update cycle.

### Commands so far
| Command | What it does |
|---|---|
| `git status` | What changed since the last commit |
| `git add <files>` / `git add .` | Choose what goes into the next commit |
| `git commit -m "..."` | Save a snapshot with a message |
| `git push` / `git pull` | Send your commits to GitHub / get new ones |
| `git switch <branch>` / `git switch -c <new>` | Change branch / create one and move onto it |
| `git log --oneline` | The history, one line per commit |
| `gh pr create --fill` | Open a pull request for your branch |
| `tools\release.bat ...` | Signing key and publishing (docs/RELEASING.md) |
