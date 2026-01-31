# Team Playbook (Template)

A playbook tells your AI workers how to behave. Copy this template and customize for your team.

---

## Team & Roles

```markdown
## Members & Projects
- @alice -> ~/backend-api
- @bob -> ~/mobile-app
- @carol -> researcher
- @dan -> operations manager
```

**Our team example:**
- 5 AI workers, each owns a project or role
- @luck is ops manager (coordinates, monitors, unblocks)
- @geni is researcher (deep dives, no specific project)

---

## Operations Manager

```markdown
## Operations Manager (@dan)
- Assign tasks, track workload, coordinate team
- "check team": Review all workers' status
- Can run: /clear, /compact, /resume (when workers are stuck)
```

**Our team example:**
- @luck checks on all workers daily
- Reassigns work when someone is blocked
- Only person who can force-reset a stuck session

---

## Non-Negotiables

```markdown
## Non-Negotiables
- Always run tests before commit/push
- Get review before merging to main
- Include your name when messaging teammates
```

**Our team example:**
- `./test.sh` must pass before any push
- Workers message each other directly (no need to go through manager)
- Always say "@lee here - ..." so teammates know who's talking

---

## Communication

```markdown
## Team Communication
- Message teammates directly (no need to go through ops manager)
- Always include your name: "@alice here - can you check this?"
- Ask ops manager only for things you cannot do yourself
```

**Our team example:**
- @geni asks @chen directly for help with research
- @lee asks @luck only when session is stuck and needs /clear

---

## Core Commands

```markdown
## Core Commands
- Start fresh: `cd ~/PROJECT && git pull origin main`
- Check cost: `/cost`
- Compact context: `/compact`
- Get advisor input: `codex exec "<prompt>"`
```

**Our team example:**
- Before complex work: discuss with Codex (3-5 turns minimum)
- Goal: 9/10 quality solution before implementing
- Search skills first: `skills-search.sh <keyword>`

---

## Learning Culture

```markdown
## Learning Culture
| Who | What | When |
|-----|------|------|
| Ops manager | Ask "any learnings today?" | Daily |
| Everyone | Share workflows, gotchas, mistakes | When asked |
| Ops manager | Curate into Evergreen Top 10 | Weekly |
```

**Our team example:**
- @luck asks daily: "Any learnings? Anything you need help with?"
- Team shares: "Never use pkill on multi-node setups - use PID-based killing"
- Weekly: best learnings go into `~/learnings/README.md`

---

## Quick Start Checklist

1. Copy this template to `~/team-playbook.md`
2. List your team members and their projects
3. Define your non-negotiables (tests, reviews, etc.)
4. Set up `~/learnings/README.md` for capturing lessons
5. Tell each worker: "Read ~/team-playbook.md"

---

**Tip:** Start simple. Add rules only when you hit real problems. The best playbooks grow from experience, not imagination.

---

## Clean Version (Copy This)

```markdown
# Team Playbook v1.0

## Members & Projects
- @worker1 -> ~/project-a
- @worker2 -> ~/project-b
- @researcher -> research
- @ops -> operations manager

## Operations Manager (@ops)
- Assign tasks, track workload, coordinate team
- "check team": Review all workers' status
- Can run: /clear, /compact, /resume (when workers are stuck)

## Non-Negotiables
- Always run tests before commit/push
- Get review before merging to main
- Include your name when messaging teammates

## Team Communication
- Message teammates directly (no need to go through ops manager)
- Always include your name: "@worker1 here - can you check this?"
- Ask ops manager only for things you cannot do yourself

## Core Commands
- Start fresh: `cd ~/PROJECT && git pull origin main`
- Check cost: `/cost`
- Compact context: `/compact`

## Learning Culture
| Who | What | When |
|-----|------|------|
| Ops manager | Ask "any learnings today?" | Daily |
| Everyone | Share workflows, gotchas, mistakes | When asked |
| Ops manager | Curate into Evergreen Top 10 | Weekly |

---
Changelog: See ~/team-changelog.md
```
