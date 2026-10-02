## Inputs needed

None — this step is blocked on a decision, not a value. The operator must choose the tech stack for the control panel before anything can be built.

---

## Decision needed before building

The control panel needs a **tech stack** — the underlying software that makes the web page work. This is the one thing you haven't chosen yet, and it determines how the panel is built, how easy it is to change later, and how it's secured for outside access.

Here's what's being chosen, in plain words:

- **What the panel is made of** — the programming language and framework that runs the web page you'll open on your phone or computer.
- **Where it runs** — it will run inside container 111 (control-panel), which already exists. The choice is about what software goes inside it.
- **Why it matters to you** — you said the panel should be *editable*: whatever is on the page should be changeable later without rebuilding it from scratch. Some stacks make that easy (change a text file, refresh the page), others make it harder (need to recompile code).

You are **not expected to know the technology**. Here are the options in everyday terms:

### Option A: A simple Python web app (Flask + plain HTML/JavaScript)

- **What it means for you:** The panel is a small program written in Python (a common, readable language). The page itself is plain web code. To change what's on the page, you edit a text file and restart the panel.
- **Trade-off:** Easiest to understand and modify if you ever want to tinker. Fewer moving parts. But the page won't feel as "modern" as some fancier options — it'll be clean and functional, not flashy.
- **Best if:** You want something simple, reliable, and easy to change later without hiring a developer.

### Option B: A modern web app (Node.js + React or similar)

- **What it means for you:** The panel is built with the same tools big websites use. It feels snappy and polished — buttons respond instantly, no page reloads.
- **Trade-off:** More complex under the hood. Changing the page later means editing more files and understanding more structure. Harder to tinker with casually.
- **Best if:** You want the panel to feel like a professional product and don't plan to modify it much yourself.

### Option C: A pre-built dashboard tool (like Homepage or Dashy)

- **What it means for you:** Instead of building from scratch, you install an existing dashboard program and configure it with a simple text file. It already has widgets for showing services, links, and status.
- **Trade-off:** Fastest to get something on screen. But it's designed for *showing* things, not *doing* things — making it actually edit Palworld settings or send requests to Radarr/Sonarr requires workarounds. Less flexible for your specific four capabilities.
- **Best if:** You want something quick and mostly for display, and are okay with the four capabilities being more limited.

### Option D: A low-code tool (like Node-RED)

- **What it means for you:** You build the panel by dragging blocks around in a visual editor. Each block does one thing (call an API, show a value, take input).
- **Trade-off:** Very flexible for connecting to other services (Pi-hole, Radarr, Palworld). But the visual editor is its own thing to learn, and the result looks more like a control board than a polished web page.
- **Best if:** You like the idea of wiring things together visually and don't care about a sleek look.

---

**My recommendation:** Option A (Python + Flask). It's the simplest thing that does all four capabilities well, it's easy to edit later (which you asked for), and it's easy to secure for outside access. But this is your call.

**If you can't choose:** Tell me what you want to be able to *do* with the panel day-to-day — for example, "I want to open it on my phone and type a movie name," or "I want to see a list of blocked ads" — and I'll turn that into a recommendation.

**Once you answer, I'll build the panel with exactly the four capabilities you asked for:**
1. Edit Palworld server settings
2. A box to request a movie or TV show (goes to Radarr/Sonarr)
3. scaffold-engine reachable in the panel (for running larger local models on the P40)
4. A read-only network view (Pi-hole queries, blocks, per-client traffic)

Nothing else will be added.
