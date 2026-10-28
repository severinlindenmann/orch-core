// The tester's checklist of e2e RUN 4 (orch v2 from the beginning), as data. Source: the lead's owner-checklist-run2.md.
// Step ids match the sections and numbers of that file (A1, B1.3, C5 ...); the server accepts ids of the form
// [A-Z]<n>[.<n>] only (testrun.py). Nothing secret belongs here: no keys, codes, invites or passphrases.

import { ADDON_SECTIONS } from './addons'

// FLIP AT THE NEXT PIN: change this one value to 'run5' (the whole page, the server path and "Done in run N" follow it).
// Steps passed in earlier runs are then carried over (see carry.ts); the step texts below may stay or be edited.
export const RUN_ID = 'run4'
export const RUN_NUMBER = Number(RUN_ID.slice(3))

export interface ChecklistStep {
  id: string
  /** Short heading; the text below it is the instruction. */
  title: string
  text?: string
  /** Sub-points, one line each. */
  points?: string[]
  /** Commands to copy, one per entry. */
  commands?: string[]
  /** What you should see ("See:" in the source). */
  see?: string
  /** A caution or a note marked with the warning sign in the source. */
  warn?: string
  optional?: boolean
  /** A heading shown above this step (a group inside the section). */
  group?: string
}

export interface ChecklistSection {
  id: string
  title: string
  where?: string
  intro?: string
  introPoints?: string[]
  introCommands?: string[]
  steps: ChecklistStep[]
}

export const RUN2_TITLE = 'Test checklist: end-to-end run 4'
export const RUN2_SUBTITLE = 'orch v2 from the beginning, with a real install of one tested build (updated 21 Oct 2026: core A to F, add-ons H to V, new checks W to T)'

export const RUN2_INTRO = [
  'You test on your Mac with a real install from develop, in NEW empty test folders. Never your real projects.',
  'Lens: you are not a developer. Note what is confusing, not only what breaks.',
  'Items marked with a warning could not be verified from code or docs. Each step shows a "next:" line when something is left; if it differs, follow it and report it.',
  'Never paste or photograph the secret words, recovery code, passphrase, QR or invite. (Test words you paste to the lead are fine.)',
  'Run order: A, B, C, D, E, then the add-ons H to V and the new checks W, X, Y, Z, S and T. F (phone) is optional and needs the iPhone ready. At the end press "Copy report" and send it to the lead. All times on screen are your local time.',
]

export const RUN2_KNOWN_GAPS = [
  'Quick tasks do not close by themselves yet (auto-close comes after this run). Close them by hand.',
  'A project folder that still has an orch store from an older version in its root (config.json, keys.jsonl, events/) is refused by setup, with a message saying what to delete. Nothing is converted.',
  'Phone pairing only works through the CLI (see F). Setup and Settings do not offer it.',
]

export const RUN2_REPORT_HELP = {
  text: 'For each step: pass or fail, BLOCKING (stops you) or non-blocking, the exact red text or "err <code>" line, the "next:" line if it differed, and a screenshot of the browser or Terminal. Never screenshot QR, link, codes, words, passphrases or the invite. Blocking = stops you: tell the lead at once (we fix, then rerun). Non-blocking = confusing or ugly but you can go on: collect them and report them together at the end.',
  points: [
    'Also report: build number and iOS version, minutes per section, any prompt or screen this list did not mention, every wording you did not understand, anything marked with a warning that turned out different, and friction that is not a bug.',
  ],
  cleanup: [
    'orch runner disable --workspace W',
    'orch host disable',
  ],
  cleanupText:
    'Clean up when finished (the host line only if the Mac is not used for other things): phone Settings, "Leave Live"; unset ORCH_GRANT; delete ~/orch-invite; the test folders may stay.',
}

const CORE_SECTIONS: ChecklistSection[] = [
  {
    id: 'A',
    title: 'Install',
    where: 'Terminal',
    steps: [
      {
        id: 'A1',
        title: 'Install with the line from the lead',
        text: 'Use the install line the lead sent you (it names the exact version). It needs uv, Node 24, git 2.32+ and builds the dashboard, about 20 s. If orch is already installed (the run 2 version), install over it, then run "orch host enable" and "orch dashboard restart" (the dashboard restart is needed once when coming from the run 2 version).',
        see: 'Installed 1 executable: orch. "orch --version" prints "orch 2.0.0.dev0 (development build)" for a source build, or the version, channel and commit for a release. If not found: uv tool update-shell, then a new Terminal.',
        warn: 'Do not use pip install git+https:// (keychain hang).',
      },
      {
        id: 'A2',
        title: 'Turn the host on',
        commands: ['orch host enable'],
        text: 'It starts the host (and restarts it on the new release). It does not restart the dashboard: "orch dashboard open /checklist" or "orch dashboard restart" does.',
        see: 'Plain lines, no error. "orch host status" shows loaded=yes running=yes. If you installed over an older version, run "orch dashboard restart" once.',
        points: ['Tip: after this, "orch dashboard open /checklist" brings you back to this page.'],
      },
    ],
  },
  {
    id: 'B',
    title: 'orch setup',
    where: 'Terminal + browser',
    intro:
      'Make two folders first, and pick ONE existing project folder (a copy of a small repo; call it P). Setup refuses your home folder, "/", and project folders that still hold an orch store from an older version in their root (config.json, keys.jsonl, events/). Try "cd ~ && orch setup" once: the refusal must be clear and name the next step.',
    introCommands: ['mkdir ~/orch-run4-a ~/orch-run4-b'],
    steps: [
      {
        id: 'B1.1',
        group: 'B1  Folder A, terminal approval, agents 3 days',
        title: 'Start setup in folder A',
        commands: ['cd ~/orch-run4-a && orch setup'],
        see: 'In Terminal: "orch setup: <folder> (new workspace)" and a copyable single-use link (valid 5 minutes). The browser opens by itself; if it does not, copy the link into your browser. The terminal stays running.',
      },
      {
        id: 'B1.2',
        title: 'Look at each step in the browser',
        text: 'The browser shows the steps: Welcome, You, Agents may, Approval, Secret words, Review. Look at each.',
        points: [
          'Welcome: your folder is named; everything stays on this computer.',
          'You: name (e.g. Severin) and short name RA (the ticket prefix, e.g. RA-0001). Continue stays grey until both are filled.',
          'What may agents do here: switches, each with a one-line description. Dashboard is on and cannot be turned off. Start agents, Terminals and Worktrees are already ticked (leave them on), and agent access of 3 days is already selected; the others are your choice. Check that the review at the end lists them.',
          'How do you want to approve things: choose Terminal. Then "Let agents work for you for": choose 3 days. Runner access: on. (Warning: a "relay and phone" option should NOT appear, it is a later add-on. If you see it, tell the lead.)',
          'Secret words: the passphrase is shown once, plus 24 numbered recovery words. Write them down on paper. Tick "I saved ...". Type the 3 words asked and press Confirm. Try one wrong word first: it says how many tries are left (5 in all; do not use them all). A wrong word clears only that field. The page never asks for the passphrase itself.',
          'Review: it lists what will be created (workspace, short name, person, folder) and what you approve (add-ons, "Agents may work for 3 days", runner access). Plain sentences first, details in a fold-out.',
        ],
      },
      {
        id: 'B1.3',
        title: 'Two confirmations, in this order',
        points: [
          'a) In the browser, type the word GRANT shown on the page.',
          'b) In the Terminal, three words appear. They must match the browser. Type yes, then your passphrase if asked.',
        ],
        see: '"Setting up your workspace" in the browser with a progress list (each Waiting, Working, Done). It may wait on "Waiting for the agent runner to connect": allow macOS "background item added" if it appears.',
      },
      {
        id: 'B1.4',
        title: 'The end of setup',
        text: 'At the end: "Your workspace is ready" with a link to the dashboard, already signed in. Today shows workspace RA. The Terminal prints a closing line and returns to the prompt.',
        warn: 'Failed or stuck: copy the red text and the last Terminal lines (blocking).',
      },
      {
        id: 'B1.5',
        title: 'Run setup again in folder A',
        commands: ['cd ~/orch-run4-a && orch setup'],
        see: '"Setup is complete ... Nothing is missing" (or "Finish setup" with only the missing steps listed). No secret-words screen. If it offers to redo things already done: report.',
      },
      {
        id: 'B1.6',
        title: 'Refresh test in folder B',
        commands: ['cd ~/orch-run4-b && orch setup'],
        text: 'Go to the Review step, then refresh the browser page.',
        see: 'The page says the session ended, and the Terminal says the page was closed or reloaded. Press Enter in the Terminal for a fresh link. You get NEW secret words (the old ones are void). If the Terminal stays silent: report (non-blocking).',
      },
      {
        id: 'B1.7',
        title: 'Ctrl-C test',
        text: 'In B, get to the Secret words screen, then press Ctrl-C in the Terminal.',
        commands: ['ls -a ~/orch-run4-b'],
        see: 'The Terminal says cancelled, the browser says "Setup paused". The ls shows no orch files.',
        points: ['Then run "orch setup" again in B and finish it as in B1.2 to B1.4 (short name RB, Terminal approval, agents 1 day). You now have a second workspace.'],
      },
      {
        id: 'B2.8',
        group: 'B2  Existing project P',
        title: 'Setup in an existing project',
        commands: ['cd P && orch setup'],
        see: 'The page names P as a new workspace. Your files stay untouched ("git status" in P: only new orch files such as an AGENTS.md block, .agents/skills, CLAUDE.md lines; nothing of yours deleted). Short name RP, terminal approval, no agents. Finish.',
        points: [
          'AGENTS.md has a clearly marked orch block and the rest is yours; CLAUDE.md imports AGENTS.md. If a file you wrote was overwritten: BLOCKING, report.',
          'Also try a project folder that still has config.json, keys.jsonl or events/ in its root (an older orch store): setup must refuse clearly and say what to delete (expected).',
        ],
      },
      {
        id: 'B3.9',
        group: 'B3  One-click (optional extra, SECOND folder, not A)',
        title: 'One-click approval in folder C',
        optional: true,
        text: 'Skip it if you prefer the terminal. One-click keeps its key in a key file only you can read on this computer.',
        commands: ['mkdir ~/orch-run4-c && cd ~/orch-run4-c && orch setup'],
        points: ['Short name RC. At the approval step choose "One click on this computer". Agents 1 day, runner on.'],
        see: 'The passphrase is NOT shown on the secrets screen, only the recovery code. The review mentions one-click signing and shows a risk notice ONCE (the only full time you see it). Finish as in B1.3 and B1.4.',
      },
    ],
  },
  {
    id: 'C',
    title: 'Dashboard tour',
    where: 'Browser; workspace RA; use "orch dashboard open" if signed out',
    steps: [
      {
        id: 'C1',
        title: 'Main pages open',
        text: 'Today, Tickets, Board, Settings open without red error boxes. No blank page on reload.',
      },
      {
        id: 'C2',
        title: 'New ticket',
        text: 'Click "New ticket", fill the text, acceptance checks and people (owner, assignee, reviewer).',
        points: [
          'Next to the key the view says "key per host" (by design).',
          'Press Create, read the review, then press Approve. The button waits about a second before it works; press it again then.',
        ],
        see: 'ONE review for everything, plain like a ticket (a headline, the text, the checklist, people by name), with "Technical details" folded away. Nothing to type. After signing the drawer closes and the new ticket opens.',
      },
      {
        id: 'C3',
        title: 'Edit a ticket',
        text: 'Open the ticket page, "Edit text, checks, repositories and people". Press Approve, then the button again after a second, as in C2.',
        see: 'ONE review for all changes (old to new is clear, e.g. "remove: Severin").',
        warn: 'This crashed before (KeyError: title): if it fails again, BLOCKING.',
      },
      {
        id: 'C4',
        title: 'Approve requirements',
        text: '"Approve requirements" is possible only when the ticket is complete (text, checks, people). When incomplete it says what is missing. Approve it in the dashboard.',
      },
      {
        id: 'C5',
        title: 'Quick task',
        text: 'Use Quick tasks, "Start task". Optional agent access: 8 h, 1 day or 3 days, never above your workspace maximum. Press Approve, then the button again after a second.',
        see: 'ONE signature for everything (ticket, approvals, agent access). The pill moves to the new end. Quick tasks do not close by themselves yet (known gap).',
      },
      {
        id: 'C6',
        title: 'Header pill: Extend, Stop, Start again',
        text: 'The pill says: "Agents can work for you until <soonest end, local time> . Extend . Stop".',
        points: [
          'Extend: choose a length, sign. Running agents stop; new ones use the new end. The pill shows the new end.',
          'Stop: sign each stop. After: "Agents stopped . Start again". Cancel a stop request: the pill must return to normal at once (report if it shows "Stopped 0 of 2").',
          'Start again: choose 8 h / 1 day / 3 days, sign (the access goes to this computer\'s agent runner). Times in these reviews are local, not "...Z".',
        ],
      },
      {
        id: 'C7',
        title: 'Settings',
        text: 'Allowed ways to approve. You approve in the dashboard: press Approve, then the button again after a second.',
        points: [
          'Phone says it needs the relay add-on (later).',
          'Boxes that cannot change are greyed with a reason: report anything that is greyed without an explanation.',
          '"My settings": your own default way. "Agents may work for you for up to": 8 h / 1 day / 3 days / 7 days (lowering never shortens access already given).',
          'Members, Grants, Devices: look; raw ids behind "Technical details" are known, other wording you do not understand is worth a note.',
        ],
      },
      {
        id: 'C8',
        title: 'One-click workspace RC',
        optional: true,
        text: 'Only if you did B3: create a ticket.',
        see: 'Approve works after about 1 s, a small "One-click" badge, no big warning banner.',
      },
    ],
  },
  {
    id: 'D',
    title: 'Core CLI',
    where: 'Terminal, in ~/orch-run4-a; ticket prefix RA; use the passphrase from setup',
    steps: [
      {
        id: 'D1',
        title: 'Basic commands',
        commands: ['orch status', 'orch list', 'orch doctor', 'orch check'],
        see: '"ok status person=Severin", no errors. If host.unavailable: orch host enable.',
      },
      {
        id: 'D2',
        title: 'Agent verbs need a grant for your shell',
        text: 'Type the word and the passphrase. Copy the printed ORCH_GRANT= value (shown once) with "export ORCH_GRANT=<value>", then run the ORCH_SESSION line below. Never paste the value anywhere else, and never into a note. You already have RA-0001 (C2) and RA-0002 (C5), so "orch new" makes the next number: RA-0003 in the lines below. If it prints another key, use that key instead of RA-0003 in D2, D3, D4 and D5.',
        commands: [
          'orch grant --days 1 --label cli',
          "export ORCH_SESSION=s_01$(python3 -c 'import secrets;print(secrets.token_hex(12).upper())')",
          'orch new "Owner CLI test" --type feature --priority high',
          'orch ac add "It works" --ref RA-0003',
          'orch section set requirements --ref RA-0003 -m "Do the thing"',
        ],
        points: ['New: --days 1..7 (--hours still works, up to 168). Do the same for the sections context and out_of_scope.'],
        see: '"ok RA-0003 ticket.created" (or the next free number), then one "ok" line per command.',
      },
      {
        id: 'D3',
        title: 'Human approval (no grant)',
        text: 'A person signs, so the grant must not be set for this command. "env -u" runs it without the grant and keeps your grant for D4.',
        commands: ['env -u ORCH_GRANT -u ORCH_SESSION orch approve requirements --ref RA-0003'],
        points: [
          'The NEW terminal view: first a headline ("Approve the requirements of RA-0003 . Owner CLI test"), then the text and checklist, then "Type RA-0003/XXXXXXXX to continue (d = details)".',
          'Press d: the long technical dump appears (hashes etc.); the plain view must not contain raw JSON.',
          'Then the plain passphrase step (names what you sign; d = details). Host, runner and grant commands print plain lines, not raw JSON.',
        ],
        see: '"ok RA-0003 gate.approved requirements"',
      },
      {
        id: 'D4',
        title: 'Lifecycle as an agent, then as a human',
        text: 'Needs the grant and ORCH_SESSION from D2 still set in this Terminal. Agent: claim, plan, verification, a task, evidence. Human: approve the plan (with "env -u", as in D3). Agent: submit. Human: verdict pass. Use your own key in place of RA-0003 if D2 printed another one.',
        commands: [
          'orch claim RA-0003',
          'orch section set plan --ref RA-0003 -m "One step"',
          'orch section set verification --ref RA-0003 -m "Run it and look at the result"',
          'orch task add "Do the thing" --proves AC1 --ref RA-0003',
          'orch task start T1',
          'echo "it works" > ~/orch-run4-a/check.log',
          'orch artifact add ~/orch-run4-a/check.log --kind log --ac AC1 --ref RA-0003',
          'orch task done T1',
          'env -u ORCH_GRANT -u ORCH_SESSION orch approve plan --ref RA-0003',
          'orch submit RA-0003',
          'env -u ORCH_GRANT -u ORCH_SESSION orch verdict pass --ref RA-0003 -m ok',
        ],
        points: [
          'The plan must be approved before "orch submit", and every acceptance criterion needs evidence (the artifact line) first.',
          'NEW: "verdict pass" now works for you as the only member, even though you claimed it (was blocking in run 1).',
          'The code gate is off by default (no "approve code"). "orch request-changes --ref RA-0003" sends it back. --dry-run tests a write.',
        ],
        see: '"ok RA-0003 claim.taken", "ok RA-0003 gate.approved plan", "ok RA-0003 ticket.submitted testing", then "verdict.given".',
        warn: 'If you still get "role.denied gate.not_eligible": BLOCKING.',
      },
      {
        id: 'D5',
        title: 'Grant, close, reopen and the rest',
        commands: [
          'orch grant --days 3 --label x',
          'orch new "Second ticket"',
          'orch close RA-0004 --resolution wont_do -m test',
          'orch inbox',
          'orch next',
          'orch search Owner',
          'orch log "A note from the checklist" --ref RA-0001',
          'unset ORCH_GRANT ORCH_SESSION',
          'orch reopen RA-0004 -m test',
          'orch grant --days 8',
        ],
        text: 'Run "orch grant --days 3 --label x", then copy the printed ORCH_GRANT= value into "export ORCH_GRANT=<value>" and run the ORCH_SESSION line from D2 again; the next five commands are agent commands and need both. "orch new" creates the next key, RA-0004 (so there is one to close; use the key it prints). The dashboard pill then shows the soonest end (local time). Reopening is a person\'s decision, so "unset" first: "orch reopen" asks for your passphrase. "orch grant --days 8" must be refused (max 7 days).',
      },
      {
        id: 'D6',
        title: 'Records (optional, 5 min)',
        optional: true,
        commands: ['orch records init', 'orch records commit', 'orch records status'],
        text: 'Settings, Records shows it. No push.',
      },
    ],
  },
  {
    id: 'E',
    title: 'Agents',
    where: 'Dashboard, workspace RA; needs claude or codex logged in on this Mac',
    intro:
      'Add-ons (start-agent, terminals, worktrees) and the runner were turned on in setup. Repos are added in Settings (section I below), so do section I before E1 if you have not yet.',
    introCommands: ['claude plugin marketplace add severinlindenmann/orch-core', 'claude plugin install orch-core@orch-core'],
    steps: [
      {
        id: 'E1',
        title: 'Make a repo to work in',
        commands: [
          'mkdir ~/orch-run4-repo && cd ~/orch-run4-repo && git init && git commit --allow-empty -m init',
          'orch show RA-0001 --full',
          `orch set RA-0001 links='{"repos":["repo"],"branches":{"repo":"feat/x"}}'`,
        ],
        text: 'In Settings, General, Repositories, add it as a repo of RA (name repo; see section I). Then link it to RA-0001: the last two commands need a grant in that Terminal (export ORCH_GRANT and ORCH_SESSION as in D2). "orch show RA-0001 --full" prints a lot: ignore it, it only has to be read first.',
        warn: 'The worktree steps follow D60 code; report what you see. The Claude plugin line above needs private repo access.',
      },
      {
        id: 'E2',
        title: 'Start an agent',
        text: 'Ticket RA-0001 (requirements approved), "Start agent": pick the harness and mode Work, Start. First time it may ask to issue a runner access: sign it.',
        see: 'No raw shell command on screen (it is folded behind "Details"); if something is missing the panel says it in words, not Terminal commands. A run appears; a terminal opens in the Terminals view; the agent runs "orch status" and "orch claim RA-0001".',
      },
      {
        id: 'E3',
        title: 'Worktree',
        commands: ['ls ~/orch-run4-repo.worktrees'],
        see: 'A folder ~/orch-run4-repo.worktrees/<WS8>-RA-0001 appears. Without it the dashboard says "shared working copy". Report which.',
      },
      {
        id: 'E4',
        title: 'The agent asks you a question',
        text: 'Tell the agent in its terminal: "ask me an option question before you finish".',
        see: 'A question on Today and on the ticket. Answer it in the dashboard (sign as in C2). The agent continues.',
      },
      {
        id: 'E5',
        title: 'Terminal view',
        text: 'Type in the agent\'s shell; open the same terminal in a second tab: read-only with "Take control". Stop a run with Stop.',
      },
      {
        id: 'E6',
        title: 'Extend while an agent runs',
        text: 'Use the pill (C6) while an agent runs: the running agent may lose its access. Report what happens to it.',
      },
    ],
  },
  {
    id: 'F',
    title: 'Phone',
    where: 'iPhone, optional. Uses workspace A (terminal approval)',
    intro:
      'Setup and Settings do not offer pairing yet: use the CLI path below (untested on a real phone). The lead prepares the TestFlight build number (newer than 0.1.0 261016.160026; it needs the B10 "Approve on your phone" screens, orch-mobile #92, and push), and the invite file for the relay: save it as ~/orch-invite and chmod 600 it. Ask the lead; single use, valid 1 h.',
    steps: [
      {
        id: 'F1',
        title: 'Install the app',
        text: 'TestFlight, Orch, the build the lead names. Open it: Welcome with "Try Demo" and "Pair your Mac". If it shows Demo data: Settings, "Leave Demo and pair a Mac".',
        see: 'Report the build number and the iOS version.',
      },
      {
        id: 'F2',
        title: 'Pairing (CLI path)',
        text: 'Find the id: "orch host list" (workspace_id = W, all 32 characters). Then:',
        commands: [
          'orch host list',
          'orch keys primary init --workspace W --relay https://relay.dev.severin.io --invite-file ~/orch-invite',
          'orch host relay link --workspace W --max-publication-bytes 4194304',
          'orch host relay primary set --workspace W',
          'orch host relay pair --workspace W',
        ],
        points: [
          'a) keys primary init: new primary passphrase twice, once more to unlock, then the 24 recovery words typed; this is a different passphrase from the workspace one. See "registered": true. primary.invite_unusable: ask for a new invite. primary.account_failed: run again.',
          'b) host relay link: type <8 hex>/GENESIS, workspace passphrase.',
          'c) host relay primary set.',
          'd) Phone: "Pair your Mac"; then run host relay pair. See a QR, an orch:// link, "valid 10 min". Scan it in the app; "Review this invitation" shows relay.dev.severin.io; Connect.',
          'e) Mac asks the phone\'s 6 characters; then primary passphrase + recovery words; then the phone\'s second 6 characters; then a review ending "Type <8 chars of the device id>" and the workspace passphrase. Phone shows "Ready" with Look and Decide, then Live Home.',
          'If the offer expires or breaks: orch host relay pair --workspace W --resume --offer O',
        ],
      },
      {
        id: 'F3',
        title: 'One-click + phone',
        optional: true,
        text: 'Only with the one-click workspace RC. This is the combination run 1 could not do (no real-phone test has been done). Pair a phone with a one-click workspace only AFTER F2 works. Steps are the same, but the Mac asks for the typed word (last 8 characters of the device id) instead of the workspace passphrase when it signs the pairing.',
        points: [
          'If an old phone app is installed: "Update orch on your phone" / reader_too_old (expected refusal).',
          'Also try turning one-click ON in Settings while the (up to date) phone is already paired: it must work; with an old app it must refuse and say why, before anything changes.',
        ],
      },
      {
        id: 'F4',
        title: 'Reads on the phone',
        text: 'Live Home shows workspace RA (no Demo chip). Questions and tickets show; pull to refresh. Settings: custody Secure Enclave, realm relay.dev.severin.io.',
      },
      {
        id: 'F5',
        title: 'Answer a question on the phone',
        text: 'Window 2, cd ~/orch-run4-a, grant for one hour, export ORCH_GRANT and ORCH_SESSION (as D2).',
        commands: ['orch grant --hours 1 --label phone', 'orch new "Phone test ticket"', 'orch ask "Pick one" --ref RA-000N --options yes,no --rec yes', 'orch show RA-000N'],
        see: 'Phone: open the question, choose, "Sign and send"; Face ID. Mac: "orch show RA-000N" shows question.answered.',
      },
      {
        id: 'F6',
        title: 'Create a ticket on the phone',
        text: 'Plus, title "Made on the phone", "Sign and create".',
        commands: ['orch list'],
        see: 'Mac: "orch list" shows it.',
      },
      {
        id: 'F7',
        title: 'Approve on your phone',
        optional: true,
        text: 'NEW, B10 phase 1: requirements and plan gates only. Make a ticket with requirements written (D2) and not yet approved. Dashboard, open the ticket, "Approve requirements": in the dialog, "Other ways" or the main button choose "Approve on your phone" (needs the relay add-on and a paired phone; Settings, Allowed ways then lets you allow Phone).',
        points: [
          'Dashboard: "Waiting for your phone..." with a 10 minute countdown, Cancel, "Use another way".',
          'Phone: a push "Needs your approval" arrives (generic text, no ticket name). Tap it. The phone shows the headline, the full text of the requirements, checklist, and lines labelled "per host, not signed" (ticket name, who may approve) plus "Requested from your Mac (not verified)". Approve with Face ID.',
          'The dashboard flips to approved by itself; phone says "Approved."; no wording such as "secure" or "verified".',
          'Also: "Request changes" (type a reason) once. Cancel a request on the Mac and see the phone list drop it.',
          'Edit the requirements text after requesting, then approve on the phone: it must say "This changed while you looked. Reopen it." and sign nothing.',
          'The code gate and the final verdict cannot be approved on the phone yet (phase 2): expect the dashboard to offer other ways only.',
        ],
      },
      {
        id: 'F8',
        title: 'Push for a new question',
        text: 'Lock the phone. In window 2 with a grant:',
        commands: ['orch ask "Push test" --ref RA-000N --options yes,no --rec yes'],
        see: 'A notification within about 30 s; tapping opens the question. If none: report the time, iOS Focus mode, and whether the question shows after opening the app.',
        warn: 'No push toggle was traced in the app Settings.',
      },
    ],
  },
]

/**
 * Core sections A-F, then the add-on sections (./addons.ts). Notes are stored by step id, so adding, renaming or
 * reordering steps never loses a note; keep an id once it has been used.
 */
export const RUN2_SECTIONS: ChecklistSection[] = [...CORE_SECTIONS, ...ADDON_SECTIONS]

export const ALL_STEPS: ChecklistStep[] = RUN2_SECTIONS.flatMap((s) => s.steps)
export const STEP_IDS = new Set(ALL_STEPS.map((s) => s.id))
export const sectionOf = (id: string) => RUN2_SECTIONS.find((s) => s.steps.some((x) => x.id === id))
