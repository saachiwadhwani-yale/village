# Village

Short-notice kid care from SOM classmates you trust.

A parent messages Village something like *"I have a coffee chat at 1 for 30 min and need someone to hang out with my kid"*. Village checks the calendars of the classmates in that parent's approved "village", asks only the ones who look free, and books the first one who says yes. It's peer coordination between classmates, not a childcare service.

## Two versions

Both run from the same app with the same agents and database.

| | **1. Web demo** (for graders) | **2. Slack** (the real product, shown in the demo video) |
|---|---|---|
| What it is | A Slack-like chat in your browser with made-up classmates | A Slack bot real classmates message |
| What you need | Python + `PORTKEY_API_KEY` | The same, plus a Slack workspace and two tokens |

**Graders: use the web demo.** Every Slack feature works the same way there.

Demo video (Slack version): *link to be added*

## How it works

```
parent message ──► intake agent ──► parent confirms the time
        ──► each villager: rules screen, or their own availability agent if unclear (in parallel)
        ──► ask 3 at a time ──► answers by button, or the reply agent reads free text
        ──► first yes wins (one atomic DB update) ──► everyone is told
```

| Agent | Job | Tools | Output |
|---|---|---|---|
| Intake | Parent's message → date, time, length, child | `get_draft_request` | `IntakeResult` |
| Availability (one per villager) | Judges unclear cases: tight handoffs, tentative events, vague written availability | `get_calendar_busy_times`, `get_stated_availability`, `get_recent_help` | `AvailabilityVerdict` |
| Reply | Reads a free-text answer: yes / no / other time / question | none | `ReplyIntent` |
| Profile | Written availability → weekly time slots, when saved | none | `ParsedAvailability` |

| Tool | Agent | What it returns | Why the agent uses it |
|---|---|---|---|
| `get_draft_request` | Intake | The request the parent is still confirming (child, start time, length, purpose, place), from the `requests` table | So a follow-up like *"actually make it 45 min"* changes the existing request instead of starting a new one |
| `get_calendar_busy_times` | Availability | Busy times on the request day from all of the classmate's connected calendars, merged, with tentative events marked and any calendar that failed to load. No event titles | To judge close calls the rules can't settle, like a class ending right at the handoff or a tentative meeting |
| `get_stated_availability` | Availability | What the classmate wrote about when they're free (marked as untrusted), plus the weekly slots and caveats the profile agent parsed from it | For classmates with no calendar, or whose note adds context, like *"I'd skip a club meeting for this"* |
| `get_recent_help` | Availability | How many times the classmate has helped this week, from the `users` table | So its explanation can note when someone has already helped a lot. It's never a reason to say no; spreading asks around is done by code |

The reply and profile agents have no tools on purpose. They read text written by classmates, so they can only return a label, and an instruction hidden in that text has nothing to act on. Everything they need is already in their prompt.

All agents use **gpt-6-luna** through Portkey. Messaging, booking, and sharing the child's allergies and notes are done by code (`backend/coordinator.py`), never by an agent.

## Using Village

Village works the same way in Slack and in the web demo: you message it in a direct message, tap buttons, and edit your profile on its **Home** tab.

- **Setting up:** the first time, Village asks a few questions in the chat. Parents give their kids' names and ages (in years, or months for babies), and can include allergies and notes in the same message, for several kids at once. Village asks at most one follow-up: about allergies if they weren't mentioned, or for anything important if nothing was shared. It then shows a summary (name, age, allergies, general notes) and asks who's in their village. Classmates who aren't parents connect a calendar or describe when they're free; parents are asked for this only once they join someone else's village.
- **Asking for help (parents):** describe what you need in plain words, like *"Accounting tomorrow at 10:10, can someone take Arjun?"*. Village confirms the time with you, then asks classmates who look free. While it searches you can ask more people at once or cancel. If someone offers a different time, you accept or decline it.
- **Helping (classmates):** tap **Yes, I can** or **Can't this time**, or reply in your own words (*"I can do 1:10 onward"*, or a question for the parent). If you say yes first, you get the kid's allergies and notes, and add-to-calendar buttons. To back out, tap **I can't make it anymore**.
- **Several villages:** a classmate can be in more than one parent's village. Once they say yes to one parent, their other open asks for that day are withdrawn, so they're never booked twice.
- **Home tab:** add, edit, or remove kids (a helper already booked gets any change to allergies or notes), village members, and calendars; edit your written availability; pause for a while; or leave a village.

## Version 1: web demo

### Run it

Needs **Python 3.11+**. Node isn't needed; the built frontend is in `frontend/dist`.

```bash
pip3 install -r requirements.txt
```

```bash
cp .env.example .env
```

Set `PORTKEY_API_KEY` in `.env` (skip this if it's already in `../.env`; the app checks this folder first, then one level up).

```bash
python3 backend/main.py
```

Open **http://localhost:8000** (or set another port, e.g. `PORT=8010 python3 backend/main.py`). The first start builds the database, `data/village.db`, from the demo data in `data/seed/`. **Reset demo** rebuilds it. The database itself isn't in the repo, since a running copy holds real users' Slack accounts and calendar links.

### Try it

Two windows each show one person in a fake Slack workspace (Village is under **Apps**). The **People** tab on the right lists everyone in three groups with how each is set up right now; open anyone in either window from there or from **Signed in as**. After a start or **Reset demo**:

| Group | Who | How they start |
|---|---|---|
| Parents | **Priya** | Arjun, 4. Villagers: Sam, Mei, Hannah (invited). |
| Classmates | **Sam** | In Priya's village. Connected to demo calendars. |
| | **Mei** | In Priya's village. Connected to demo calendars. Availability described. |
| | **Hannah** | Invited to Priya's village. Connected to demo calendar. |
| New to Village | **Marcus**, **Diego**, **Jordan** | Not set up yet. |

1. **Ask as Priya.** Send *"I have a coffee chat at 1 for 30 min and need someone to hang out with my kid"* and tap **Find someone**. The **Behind the scenes** tab shows each classmate screened by rules (⚙) or by their own agent (✦).
2. **Answer as a classmate.** Window 2 switches to someone who was asked. Tap **Yes, I can**, or type a reply like *"I can only do 1:10 onward"*. Both sides get the booking and add-to-calendar buttons; the helper gets Arjun's allergies and notes.
3. **Grow the village.** As Hannah, tap **Join**. Set up Diego (**No, I'm here to help** → **Use my demo calendar (demo only)**), then add him from Priya's **Home** tab (**Add people**) and tap **Join** as Diego. Set up Marcus as a parent, or Jordan by describing availability in words.
4. **Back out.** Book something for tomorrow, then tap **I can't make it anymore** as the helper (if it's less than a day away, confirm with **Yes, I have to cancel**). Village finds a replacement on its own.

**+ New classmate** adds a brand-new person with no demo calendar. **Reset demo** puts everyone back as in the table, with no help history, and also starts any real Slack users over from setup.

**Demo calendars:** `.ics` files in `data/seed/calendars/`, with events that repeat daily so the demo works on any day. For Priya's 1pm request:
- **Sam** is busy on his second calendar → skipped by rules.
- **Mei** has a tentative meeting she'd skip → her agent decides.
- **Hannah** (once she joins) is free → rules say yes.
- **Diego** (once set up and added) has a class ending right at the handoff → his agent decides.

**Rules:**
- Commitments up to 80 minutes, plus a 5-minute handoff on each side.
- People who look free are asked before "maybes", then whoever has helped least this week. If no one answers in 10 minutes, the next 3 are asked.
- Helpers can back out with 24h+ notice in one tap, with 6–24h after confirming, and not under 6h.
- Anyone who said no or is already helping isn't asked again that day.

### Demo vs. real use

| | Web demo | Real use (Slack) |
|---|---|---|
| People | Made-up classmates; you play all of them | Each classmate on their own Slack account |
| Calendars | Demo calendar files, connected from the seed or with one tap during setup (real links and written availability work too) | Their own private calendar link from Google or Outlook, or written availability |
| Starting point | Some people already set up, some new (see the table above) | Everyone sets up when they first join |
| Picking a village | A list of the demo people | Slack's people picker |
| Messages | Appear in the browser windows | Slack DMs from the Village bot |
| Reset | **Reset demo** restores the starting point | No reset; people edit their profile on the Home tab |

Both use the same agents, rules, and real clock. The demo calendars repeat daily so the demo works on any day.

## Version 2: Slack

The bot runs inside the same app via Slack's Socket Mode, so no hosting is needed, but it's only online while the app runs.

1. Do the first two steps of [Run it](#run-it).
2. Create a free workspace at https://slack.com/get-started (or use your own).
3. At https://api.slack.com/apps: **Create New App** → **From an app manifest** → paste [`slack/manifest.yml`](slack/manifest.yml).
4. **Basic Information** → **App-Level Tokens** → generate one with scope `connections:write` (starts with `xapp-`).
5. **Install App** → **Install to Workspace** → copy the **Bot User OAuth Token** (starts with `xoxb-`).
6. Add both to `.env` as `SLACK_APP_TOKEN` and `SLACK_BOT_TOKEN`, then run `python3 backend/main.py`. It prints `Slack: connected`.
7. Invite classmates. Village messages each new person when they join and walks them through setup (see [Using Village](#using-village)).

Slack users and web demo people can be in the same village, so you can test alone by answering for demo people in the browser.

## Tests

```bash
python3 -m pytest
```

`test_rules.py` and `test_slack_setup.py` need no model or Slack connection. `test_agents_live.py` calls gpt-6-luna (full flows, prompt-injection defenses) and needs `PORTKEY_API_KEY`; skip it with `-m "not live"`. Tests use their own copy of the demo data in `tests/seed/`.

## Project layout

```
backend/          FastAPI app; coordinator.py (workflow), onboarding.py (setup chat),
                  agents/ + prompts/, screening.py (rules), calendars.py, channels/ (web + Slack)
frontend/         React + TypeScript + Vite (built copy in frontend/dist)
data/seed/        demo people and calendars
tests/            pytest suite
slack/            Slack app manifest
scripts/, output/ measure_caching.py and trace_run.py, with results (caching_experiment.md, traced_run.md);
                  every agent run's tokens and cost are logged locally to output/audit_log.jsonl and /api/usage
```

## Safety

- **Privacy:** a real calendar is stored only as its private link. Village downloads it when checking availability, keeps only busy times in memory, and never saves event details, so agents never see event names. A child's allergies and notes go only to the helper, after they say yes. Add-to-calendar links are signed and leave them out.
- **Prompt injection:** classmates' text is marked as untrusted data in every prompt. The reply and profile agents have no tools, the availability agent's tools only read that one classmate's data, and booking is done by code.
- **Double booking:** the first yes wins in one conditional database update.
- **Cost:** per-agent limits and a token budget per request.
- **Calendar links:** https only, no private network addresses, 5 MB cap.
