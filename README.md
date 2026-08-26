# ☕ CoffeeOS — an operating system for a coffee shop

[![tests](https://github.com/danill3gacy/pekaros-app/actions/workflows/ci.yml/badge.svg)](https://github.com/danill3gacy/pekaros-app/actions/workflows/ci.yml)
[![Python 3.11+](https://img.shields.io/badge/python-3.11%2B-blue)](https://www.python.org/)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](LICENSE)
[![tests: 144](https://img.shields.io/badge/tests-144-success)](tests/test_core.py)

A live system for a single site: **a Telegram bot + a database + calculations driven by till
receipts + an automatic morning briefing + a web dashboard on real data**.

## Headline numbers

| Measure | Value |
|---|---|
| Effect in a 150-day simulation × 5 independent guest streams | **+₽5.1k/month** (range +2.6…+7.2) against an owner deciding "by yesterday" |
| Sales lost to an empty display case | **457 → 334 units/month** (−27%) |
| Service level (the guest found what they came for) | **33% → 47%** |
| The "two-week average in a spreadsheet" alternative | **−₽53k/month** — it systematically under-orders |
| Running with no manual waste entry at all | the effect holds: **+₽5.2k/month** |
| Codebase | ~9,100 lines of Python, 23 modules |
| Tests | **144**, in CI on Python 3.11 and 3.12 |
| Setup by the owner | ingredient prices only, **~5 minutes, once** — the recipe book ships ready |

> The figures come from a simulator with an **independent** demand model — a different hourly
> profile, a different weekly rhythm, seasonal drift and spikes. It is capable of showing that the
> product is useless, and it has done so once already (CHANGELOG 5.0.0).
> Reproduce it with: `python tools/simulate.py --days 150 --seeds 5 --supply`.

## The problem

The till reports turnover, not money. Turnover answers none of the questions profit actually
depends on: how much you make on a particular cup, how much you lose to an empty display case and
a morning queue, and exactly when the beans will run out. Guests who walked away are by definition
absent from the till, and the surcharge for alternative milk was set once and never touched again.

One system per site: **a Telegram bot + SQLite + calculations from receipts + an automatic morning
briefing + a web dashboard**. All four questions are answered from the same data — the receipts —
with no technical recipe cards and no manual stock-keeping.

| Area | What it gives you |
|---|---|
| Cup economics | Cost and margin for every item, from receipts and ingredient prices |
| Display case | Tomorrow's order, allowing for sales having been capped by availability |
| Purchasing | A supplier request: "you run out of beans on Thursday, order 14 kg today" |
| Shift | Load by hour and attach rate — diagnostics, not a forecast |
| Interface | The bot (10 commands, one-line entry), the dashboard, the morning briefing, backups |

**Why this can be computed at all.** Between flour and a baguette stand dough, proofing and the
baker's hands — consumption cannot be recovered from receipts. Between beans and a latte stands
nothing: the recipe is constant, so consumption and cost follow from sales.

## Engineering decisions

- **The product does not take its own word for it.** The simulator with its independent demand
  model ships as part of the product, not as a marketing slide; its job is to refute.
- **Robustness in real operation.** The system performs the same if waste is never entered at all.
  Anything resting on a nightly manual entry dies within a month, along with the barista trained
  to do it.
- **Forecast is kept separate from diagnostics.** The display-case order and the supplier request
  are a testable forecast. Margin, food cost and attach rate are diagnostics: they show the owner
  what they could not see, and the owner makes the call.
- **Autonomy.** The LLM is optional, including a free local model; with no keys and no internet the
  product works in full.

## Stack

Python 3.11, SQLite (WAL), python-telegram-bot, FastAPI/uvicorn. One-command install
(`install.sh`), automatic receipt loading, systemd units for 24/7 operation, backups.

## Testing the claims: don't take our word for it

A claim you cannot refute is worth nothing. So the product ships with a simulator built on an
**independent** demand model — a different hourly profile, a different weekly rhythm, seasonal
drift and spikes. It is capable of showing that the product is useless, and it has done so once
already (see CHANGELOG 5.0.0).

```bash
python tools/simulate.py --days 150 --seeds 5 --supply
```

Over 150 days, averaged across 5 independent guest streams:

| display-case strategy | vs the owner's own judgement | undersold, units/month | service level |
|---|---|---|---|
| two-week average (a spreadsheet) | **−₽53k/month** | 946 | 10% |
| the owner's judgement (the status quo) | 0 | 457 | 33% |
| CoffeeOS | **+₽5.1k/month** (from +2.6 to +7.2) | 334 | 47% |
| CoffeeOS with no waste entry at all | +₽5.2k/month | 328 | 48% |

Here is how to read that.

**The spreadsheet average is worse than nothing** — it systematically under-orders, because it
cannot know that sales were capped by availability. It is the most common "stock system" in coffee
shops, and it costs the owner money.

**CoffeeOS's gain over an owner who simply looks at yesterday is modest: a few thousand roubles a
month.** We deliberately do not print a prettier figure. What matters here is elsewhere: undersold
units fall from 457 to 334 a month, and the service level rises from 33% to 47% — meaning the guest
walks out without breakfast less often.

**The last row is the most important one operationally.** It shows the system performs the same if
waste is never entered at all. A product resting on a nightly manual entry stops working within a
month, along with the barista who was trained to do it.

**What the simulator does NOT prove.** It tests the display-case order and the supplier request —
the things that can be modelled. Margin, food cost, attach rate and shift load are **diagnostics,
not a forecast**: they show the owner what they had not seen, and the owner makes the call.
Promising an uplift on those would be a lie.

The `--supply` flag tests the second claim separately: if you order each day whatever the request
marked "urgent" or "soon", the shop does not run out of beans, milk or cups on a single day.

---

## What it does

| Section | What it shows |
|---|---|
| **Margin and menu** | Cost and margin per item, food cost, the menu split into stars / plowhorses / puzzles / dogs |
| **Milk** | What you earn on dairy versus plant milk — and whether the surcharge covers the difference |
| **Tomorrow's display case** | How much of what to put out so you are not empty by midday |
| **Lost revenue** | How much was missed because food ran out, and at what time |
| **Supplier request** | Consumption of beans, milk, syrups and cups from receipts; how much to order and when |
| **Food with coffee** | Attach rate, the gap between morning and afternoon, what people actually buy together |
| **Shift** | Where you hit your speed ceiling and whether a second barista is warranted |
| **What people drink** | Drinks, sizes, milk, the share of cold drinks and its seasonality |
| **Waste** | The display case at menu prices and poured-away milk at cost — kept separate |
| **Till** | Cash/card reconciliation, revenue by category |
| **Maintenance** | The espresso machine and grinder schedule: what is due |

The morning briefing arrives in Telegram every day before opening.

**The smart assistant:** the owner types a question to the bot in plain language — "what's the
margin on a raf?", "how long will the beans last?", "when is the peak?", "why the dip on
Wednesdays?" — and gets an answer from their own data. Common questions the bot computes itself,
instantly; anything unusual goes to the AI, which is handed a slice of all the shop's data
(optional; with no AI, the common questions and every button still work).

**Production features:** self-monitoring health checks (the bot warns you itself if the data stops
updating), nightly backups, weekly summaries, a command menu, error handling and logging, 144 tests.

---

## What the product does NOT do

An honest list, so there are no surprises:

- **It does not compute profit.** Cost here means raw ingredients per the recipe only. Rent,
  wages, taxes and card-processing fees are unknown to the system, and margin is never passed off
  as net profit.
- **It does not measure people who left the queue.** They are by definition not in the receipts.
  The product shows when you are hitting your speed ceiling and gives an estimate of the gain from
  a second barista — **explicitly labelled as a scenario**, with its assumption spelled out.
- **It does not compute guest retention without loyalty cards.** If the till does not pass a guest
  identifier, the section honestly says it does not know.
- **It does not run full stock accounting.** A stocktake is not required: consumption is computed
  from receipts. Recounting what is on the shelf sharpens the picture ("enough for 2 days"), but
  the product does not demand it daily — a product resting on manual entry dies along with the
  barista who was trained to do it.
- **It does not predict weather or public holidays.** Only the shop's own sales history. The
  seasonality of cold drinks is visible from that same history.
- **It does not work with "Chestny ZNAK" product labelling** (the Russian mandatory goods-marking
  system). That is a separate project.
- **One site per deployment.** `venue_id` is in the schema, but multi-site and multi-tenant setups
  are not supported: one database, one bot, one `.env`.

---

## What you need to run it in production

1. **A bot token** — Telegram → [@BotFather](https://t.me/BotFather) → `/newbot`.
2. **The `chat_id` of the briefing recipients** — start the bot, send `/start`, and it shows the id.
3. **A receipts export** covering 1–2 months (Excel/CSV) from the till or the fiscal data operator.
4. **A server** — any cheap VPS (~₽300–600/month). Data stays in Russia, per Federal Law 152-FZ.
5. *(optional)* a local Ollama model — this enables free-form questions. **Everything works without it.**

## One-command install

```bash
bash install.sh
```
The script installs the dependencies, creates the environment and `.env`, generates a password for
the web dashboard and — on a server, run as root — sets the services to start automatically.

## Manual install

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
nano .env            # BOT_TOKEN, BRIEF_CHAT_IDS, VENUE_NAME

python -m coffeeos seed                        # demo data (90 days)
# OR your real receipts:
python -m coffeeos import выгрузка.csv --reset # выгрузка.csv = your till export file
```

## Running it

```bash
python -m coffeeos bot     # Telegram bot (the main interface + the morning briefing)
python -m coffeeos web     # web dashboard → http://SERVER:8000
```

Checking it without Telegram (the subcommands are transliterated Russian; glosses on the right):

```bash
python -m coffeeos brief         # morning briefing
python -m coffeeos marzha        # margin, food cost and the menu breakdown
python -m coffeeos vitrina       # tomorrow's display case
python -m coffeeos zakupki       # supplier request
python -m coffeeos smena         # shift load
python -m coffeeos upusheno      # lost revenue
python -m coffeeos nedelya       # the week's results
python -m coffeeos status        # system health
python -m coffeeos ask "какая маржа у латте"   # "what's the margin on a latte"
python -m pytest -q              # core tests
python tools/simulate.py         # test the claims in simulation
```

---

## How cost is computed

The drinks recipe book ships ready (`coffeeos/reference.py`) and is expanded into a `recipes` table
for every size. Then, for each receipt line:

1. The item is **parsed**: "Iced latte 450 with oat + syrup" → latte, size L, oat milk, cold,
   modifier "syrup".
2. The recipe for that size is taken, the **milk is substituted** for whatever the receipt says,
   and modifiers are added on top.
3. If the receipt says "dine in", disposables are removed from the calculation.
4. Every ingredient is multiplied by its own price per gram / millilitre / unit.

**Prices are labelled.** Until the owner has confirmed their own price, it counts as a typical one
and the product says "some prices are typical, not yours". If a price is **unknown** (the purchase
price of a display-case item), the cost is not shown at all — `None`, not zero. Zero would mean
"100% margin", and the owner would take that as the truth.

To correct a price, type `цена зерно 1800` ("price beans 1800") in the bot. To see what is missing,
type `цены` ("prices").

## How the display-case order is computed

The display case is the only thing that can run out: a drink is made to order.

**1. Sales are not demand.** If the croissants ran out at 11:20, sales only show what you managed
to put out. Such days are found from the receipts (sales stopped earlier than *that item's* usual
hour while guests were still coming), and demand is recovered via the conditional expectation of a
truncated distribution.

**2. Day of week applies as a shrunk adjustment, not a hard rule.** One Saturday with a street fair
does not triple the order, but eight Saturdays are not ignored either.

**3. The buffer comes from a target service level.** Order = mean demand + z·sigma (70th percentile
by default, configurable via `CASE_SERVICE_LEVEL`). A missing unit costs more than an unsold one:
the first is a guest who will have breakfast elsewhere tomorrow, the second is only leftover stock.

Manual waste entry is **not required**: selling out is visible from the receipts. A dedicated test
verifies this.

## How the supplier order is computed

Consumption is computed exactly — from cups sold and the recipe book, with no stocktake. Then:

- **lead time affects WHEN to order, not HOW MUCH.** Add it to the volume every time and stock
  grows with every cycle;
- **perishables are capped at a sensible level** — a two-week supply of milk is not a supply, it
  is waste;
- **the day-of-week profile is accounted for**: a Friday request covering the weekend and a Tuesday
  one are different volumes;
- **recounting the shelf is not required.** Without it the request is built from consumption; with
  it, the system says "enough for 2 days, and delivery takes 3 — this is urgent".

To record what is on the shelf, type `остаток зерно 4` ("stock beans 4") in the bot.

## What the "Shift" section does and does not do

A coffee shop is constrained not by stock but by hands. Measuring people who left the queue is
impossible from receipts, so the product does what can be done honestly:

1. it computes **the shop's own throughput** — how many cups per hour this shop puts out when it is
   pushing (a high percentile of its own hours, not an industry benchmark);
2. it finds the hours where it regularly runs at that ceiling **and** where there is a genuine rush
   (an hour at least 1.4× above the median);
3. it **verifies the ceiling from the distribution**: if an hour is merely popular, the cup count
   in it varies day to day; if hands are the constraint, the distribution is clipped at the top and
   the same maximum comes out day after day;
4. and only if the ceiling is confirmed on at least a quarter of days does it estimate the gain
   from a second barista — **with the assumption written out explicitly**.

The difference between "you are losing ₽40,000 to the queue" and "in these two hours you hit the
ceiling on 43% of days; if a second barista raises throughput by 20%, that is around ₽40,000 a
month — provided the queue waits" is the difference between a product and a promise.

---

## Loading real receipts

`import_receipts` understands exports from a range of Russian till systems (Poster, iiko,
r_keeper, Evotor, Quick Resto, fiscal data operators, 1C). It finds columns by keyword: date/time,
item name, **modifiers**, **size**, quantity, price/total, receipt number, payment type,
**employee**, **guest**, **order type**. The delimiter (`;`, `,`, tab, `|`) is detected
automatically, as is the encoding (UTF-8, cp1251).

```bash
python -m coffeeos import poster_july.csv --reset
```

Modifiers held in a separate column are glued onto the item name before parsing: "Latte" + "oat
milk" is a latte on oat, and it costs more.

The menu catalogue is built from the receipts **automatically**. If an item is classified wrongly,
correct it with a single phrase (`витрина сырники` — "display case: syrniki"), then rebuild the
parse over the whole history:

```bash
python -m coffeeos rescan
```

If your columns are named unusually, adjust `COLUMN_HINTS` in `coffeeos/import_receipts.py`, and
the drinks dictionary in `coffeeos/menu.py`.

## What the system does NOT treat as a product

Service lines from the till (bags, discounts, deposits, gift cards, refunds) are kept out of the
calculations, and refunds are subtracted from revenue. **Add-ons** are recognised separately ("Syrup,
caramel" or "Oat milk" as their own line): without that, the "coffee + food" attach rate would be
inflated twofold. The keyword lists are `NON_PRODUCT_HINTS` and `ADDON_WHOLE` in `coffeeos/menu.py`.

---

## One-line entry (in the bot)

The bot takes plain Russian; English glosses on the right.

```
списание круассаны 4        — write off display-case stock (at menu price)
вылил молоко 1,5            — write off milk (at cost)
остаток зерно 3             — recounted stock, in kg / litres / units
цена зерно 1800             — your own purchase price per pack
сделал калибровку           — record a maintenance job
витрина сырники             — this item belongs in the display case
не витрина вода             — remove it from the display-case order
каталог · цены · обслуживание   — catalogue · prices · maintenance
```

## Backups

The database with the full history is copied automatically every night; the last `BACKUP_KEEP`
copies are kept (14 by default) in the `backups` folder.

```bash
python -m coffeeos backup     # a copy right now
python -m coffeeos backups    # list the copies
```
To restore: stop the bot, copy the file you want from `backups` over `coffeeos.db`, start the bot.

## A password on the web dashboard

On a production server you must set these in `.env`:
```
WEB_USER=owner
WEB_PASSWORD=your_password
```
Otherwise anyone who knows the address can see the shop's revenue. `install.sh` generates a
password automatically.

---

## A free model, no OpenAI needed (workable from Russia)

The common questions and every button work **free and with no AI at all**. AI is only needed for
the rare, unusual question.

### Option A (recommended) — a free local model (Ollama qwen2.5:3b)

The data never leaves the machine (which also helps with Federal Law 152-FZ), there is nothing to
pay, and it works from Russia without restrictions. Needs **4+ GB of memory**.

```bash
curl -fsSL https://ollama.com/install.sh | sh
ollama serve
ollama pull qwen2.5:3b
```

These lines are already in `.env` by default:

```
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen2.5:3b
LLM_API_KEY=ollama
```

Check it with `python -m coffeeos llm` — it reports 🟢/🔴 and exactly what to do. The same status is
visible in `status` and in the bot via `/status`.

If Ollama is not running or the model has not been pulled, the bot does not go mute: the buttons and
common questions are computed as usual, and a free-form question gets an intelligible reason back
rather than a generic stub.

### Option B — the free tier of Groq / OpenRouter

```
LLM_BASE_URL=https://api.groq.com/openai/v1
LLM_MODEL=llama-3.1-8b-instant
LLM_API_KEY=your_key
```
(Signing up may require a VPN from Russia; the data goes to a service abroad.)

### Option C — no AI at all

Leave `LLM_*` and `OPENAI_API_KEY` empty.

---

## Automatic receipt loading

To keep the data fresh without manual commands, enable automatic loading in `.env` (`SYNC_MODE`).
Duplicates are excluded by receipt number, so re-loading is safe.

**`folder` mode** (works out of the box):
```
SYNC_MODE=folder
SYNC_FOLDER=/opt/coffeeos/vygruzki
SYNC_INTERVAL_MIN=60
```

**`http` mode** (a direct connection to the till or fiscal data operator):
```
SYNC_MODE=http
SYNC_URL=https://<your till's API address>/receipts
SYNC_TOKEN=<access key>
```
Field names can be adjusted where needed in `coffeeos/sync.py` → `FIELD_ALIASES`.

Test one cycle with: `python -m coffeeos sync`.

## Keeping the bot online 24/7 (systemd)

`/etc/systemd/system/coffeeos-bot.service`:

```ini
[Unit]
Description=CoffeeOS bot
After=network.target

[Service]
WorkingDirectory=/opt/coffeeos
ExecStart=/opt/coffeeos/venv/bin/python -m coffeeos bot
Restart=always
User=coffeeos

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable --now coffeeos-bot
# the same for coffeeos-web.service, with ExecStart=... -m coffeeos web
```

---

## Project layout

```
coffeeos/
  config.py           settings from .env
  db.py               SQLite schema and migrations (PostgreSQL when it needs to scale)
  menu.py             parsing a receipt line: drink, size, milk, add-ons, hot/cold
  reference.py        the library of recipes, ingredients and the maintenance schedule
  costing.py          cost per serving, margin, food cost, menu breakdown
  supply.py           ingredient consumption, reorder point, supplier request, schedule
  demand.py           display case: demand, sell-outs, the order, lost revenue
  economics.py        the target service level for the display case
  analytics.py        sales, hours, days, attach rate, basket, drinks, guests
  staffing.py         throughput, hitting the ceiling, the second barista
  catalog.py          menu catalogue, waste, rebuilding the parse
  orchestrator.py     request routing + wording + briefings
  bot.py              the Telegram bot
  webapp.py           FastAPI: /api/summary + the live dashboard
  import_receipts.py  the receipts loader for till / fiscal-operator exports
  seed.py             demo data generator for a coffee shop
tools/
  simulate.py         testing the product's claims in simulation
live.html             the live-data web dashboard (served by the server)
```

<sub>The bot, the dashboard, the briefings and the code are in Russian — CoffeeOS is built for a
Russian coffee shop owner.</sub>
