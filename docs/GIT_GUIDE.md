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

---

## Lesson 5: your own relay server on Cloudflare (for rooms, version 0.4)

**Goal:** put the relay (the `relay` folder) online, so friends can join rooms
from anywhere. You set it up once; after that it updates itself whenever the
`relay` folder changes on `main`.

*Why your own:* free, private (only SyncWatch uses it), and it runs on
port 443 like any website, so it works on every network. Everything that passes
through it is encrypted by the apps, so even Cloudflare can't read it.

### 1. Cloudflare account (free, no card)
Sign up at **dash.cloudflare.com/sign-up** and confirm your email.

### 2. Your workers.dev name
In the dashboard, open **Workers & Pages** (Ctrl+K and type it, if you can't see it).
If it asks you to choose a **workers.dev subdomain**, pick one, e.g. your
nickname. Your relay's address will be `https://syncwatch-relay.<that name>.workers.dev`.

### 3. Deploy: pick one way
Both give the same result. **Way A** is fewer steps; **way B** keeps everything
in GitHub (and prints the address in GitHub). Don't set up both.

#### Way A: Cloudflare builds it straight from your repository
1. **Workers & Pages** → **Create application** → **GitHub** (connect your GitHub
   account if asked, and allow it the `syncwatch` repository) → choose `syncwatch`.
2. Settings before the first deploy:
   - **Project name** (the Worker's name): `syncwatch-relay`. It must be exactly the
     `name` in `relay/wrangler.jsonc`, or the build fails.
   - **Root directory** (sometimes under *Advanced settings*): `relay`.
     *Why:* the repository holds the whole app; only that folder is the relay.
   - **Build command**: leave it empty. **Deploy command**: `npx wrangler deploy`.
3. **Save and Deploy**. When the build finishes, its last lines show the
   address (`https://syncwatch-relay.<your name>.workers.dev`).
4. The Worker → **Settings** → **Build**:
   - **Branch control**: clear **Enable Preview Builds**.
     *Why:* pull requests don't need their own copy of the relay (the GitHub
     check *Relay works* already tests the relay code); otherwise every pull
     request also gets a Cloudflare check that can fail for no useful reason.
   - Optional, so the app's own changes don't start relay builds:
     **Build watch paths** → *Include paths*: `relay/*`.

Already created it with other settings? Change them under the Worker →
**Settings** → **Build**, then open the failed build (the Worker's
**Deployments** or builds list) and retry it, or wait for the next change to
the `relay` folder on `main`.

#### Way B: GitHub deploys it (`.github/workflows/relay.yml`)
1. Note your **Account ID**: on the Workers & Pages page, under **Account
   details**, click the copy button next to it.
2. A key for GitHub ("API token"): Ctrl+K → **API Tokens** → **Create Token** →
   template **Edit Cloudflare Workers** → **Use template**. Under *Account
   Resources* choose your account; under *Zone Resources* leave **All zones**
   (you have none). → **Continue to summary** → **Create Token**. Copy the token
   now: Cloudflare shows it only once.
   *Why:* this token lets GitHub deploy the relay to your account and nothing else.
3. GitHub → your repo → **Settings** → **Secrets and variables** → **Actions** →
   **New repository secret**, twice:
   - Name `CLOUDFLARE_API_TOKEN`, value: the token
   - Name `CLOUDFLARE_ACCOUNT_ID`, value: the Account ID

   *Why secrets:* GitHub stores them encrypted, never shows them again (not even
   to you), hides them in logs, and gives them only to workflows in your own repo.
   They never go into the code.
4. GitHub → **Actions** → **relay** (left list) → **Run workflow** → branch `main`
   → **Run workflow**. After a minute or two, open the run: the **Summary** shows
   *"The relay is online"* and its address.

### 4. Check it
Open `https://syncwatch-relay.<your name>.workers.dev/v1/health` in your browser.
It should show `{"ok":true,"service":"syncwatch-relay","version":1}`.

### 5. Tell SyncWatch the address (your own pull request, like lesson 3)
```
git switch main
git pull
git switch -c set-relay
tools\release.bat set-relay https://syncwatch-relay.<your name>.workers.dev
git add syncwatch/net/relay_url.py
git commit -m "Use my relay"
git push -u origin set-relay
gh pr create --fill
```
`set-relay` first checks that the address really answers as a SyncWatch relay.
Merge when the checks are green. Then publish the next version (lesson 4):
`publish` refuses to release rooms without a relay address, so friends never
get a version that can't connect.

### If something goes wrong
| Message | What to do |
|---|---|
| `Could not detect a directory containing static files` (way A) | The **Root directory** isn't `relay`. Fix it in Settings → Build and build again. |
| A red ✕ *Workers Builds: syncwatch-relay* on a pull request (way A) | Preview builds are on: clear **Enable Preview Builds** (way A, step 4). It doesn't block merging. |
| The build says the Worker name doesn't match (way A) | Name the Worker `syncwatch-relay`, or tell Claude the name so `relay/wrangler.jsonc` can match it. |
| *No Cloudflare secrets, so GitHub didn't deploy the relay* (way B) | The secrets from way B step 3 are missing or misspelled. With way A this note is expected. |
| `You need to register a workers.dev subdomain` | Do step 2, then deploy again. |
| `Authentication error` / `10000` (way B) | The token is wrong or expired: make a new one and update the secret. |
| `set-relay` says the relay didn't answer | Check the address (step 4); wait a minute after deploying. |

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
| `tools\release.bat ...` | Signing key, publishing, relay address (docs/RELEASING.md) |
