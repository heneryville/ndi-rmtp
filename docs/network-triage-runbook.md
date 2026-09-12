# Field runbook — "a virtual NDI device is consuming the network"

North building. Read the first section before you walk in; it's the part that
changes how the conversation goes.

---

## The claim is three claims, and only one of them is verified

> "One guy has a virtual NDI device and it's consuming the network."

| # | Claim | Verified? | How to settle it |
|---|-------|-----------|------------------|
| 1 | The network is actually saturated | **No** | §2 of the triage script — interface counters |
| 2 | A virtual NDI device exists on the LAN | Probably | §3 — NDI source census |
| 3 | (2) is what causes (1) | **No** | The A/B test below. Nothing else proves it. |

Almost every time this report reaches someone, it's claim 2 observed and claim 3
assumed. Don't inherit the assumption — you'll spend the visit fixing the wrong
thing and the problem will still be there next Sunday.

### The physics that makes this non-obvious

**NDI is unicast by default. An advertised source nobody is receiving costs
essentially nothing** — just small mDNS beacons on UDP 5353. So "he has a
virtual NDI device running" is not, by itself, a bandwidth event. Cost appears
only via one of two mechanisms:

- **Someone receives it.** Full-bandwidth NDI (SpeedHQ) is **~100–160 Mbps for
  1080p60, per receiver.** Three receivers = 400+ Mbps. On a 1 Gb LAN that is
  real damage.
- **Multicast sending is enabled.** The sender emits one copy, but a switch
  without IGMP snooping **floods it out every port** — to machines that never
  asked for it. This is the mechanism by which *one* device takes down a whole
  flat building network. Church LANs are typically flat and unmanaged, so this
  is the top suspect. It is off by default; someone had to turn it on.

For contrast, our own traffic is small: the camera is **NDI HX3** (H.265,
~8–25 Mbps in) and our YouTube push is **6.2 Mbps out** at `high`.

### "Virtual NDI device" is ambiguous — and the two meanings have opposite fixes

| What it says in the source list | What it actually is | Where the cost lands |
|---|---|---|
| `(Scan Converter)`, `(Test Pattern)`, `(OBS)`, `(vMix …)` | **Sender**, full bandwidth | The LAN |
| `(Screen Capture HX)` | Sender, H.264, moderate | The LAN |
| `(NDI Virtual Input)` | **Receiver** — turns NDI into a webcam | **The camera's uplink** |

That last row matters. If he's running **Virtual Input pointed at the chapel
camera**, he isn't flooding the LAN at all — he's adding a **third receiver** to
a small PTZ camera that already serves two of ours (`ndi-streamer` plus the
cyndilib preview/PTZ receiver — see CLAUDE.md, "PTZ preview shares the
PtzController's receiver"). That degrades *our* feed without the network being
busy at all. Completely different fix.

---

## Before you leave

- [ ] Laptop that can join the **north** building LAN
- [ ] `brew install sshpass` if not already (for non-interactive runs)
- [ ] Creds and hosts are in `ssh.md` — north is `snowspringsnorth.local`
- [ ] Camera is `192.168.111.248` (digest login)
- [ ] Copy the script over, or pipe it in:

```sh
sshpass -p "$PI_PASS" ssh pi@snowspringsnorth.local 'sudo bash -s' \
    < scripts/network-triage.sh | tee north-triage-before.txt
```

**Open question to answer on arrival:** the Pi is `192.168.108.207` and the
camera is `192.168.111.248` — different /24s, yet NDI discovery (mDNS, which
does not cross subnets unaided) works between them. So either the church LAN is
one wide flat supernet (e.g. `/22`, covering .108–.111) or there's an mDNS
repeater. **A large flat L2 broadcast domain is exactly the environment where
one multicast sender floods everyone**, so this materially raises or lowers the
multicast hypothesis. §0 of the script prints the netmask — check it first.

---

## Step 1 — Establish the symptom (do this before touching anything)

"Consuming the network" is an interpretation, not an observation. Ask what
someone actually *saw*, then branch:

| Reported symptom | Most likely cause | Go to |
|---|---|---|
| YouTube stream choppy / buffering for viewers | **WAN uplink**, not LAN NDI | Step 5 |
| Camera missing from the NDI dropdown; "Timed out waiting for first frame" | Camera receiver exhaustion, or discovery | Step 4 |
| Building internet slow for *everyone*, not just us | **LAN flood — multicast** | Step 3 |
| Preview pane freezes, stream itself is fine | Camera overloaded by extra receivers | Step 4 |
| "Someone told us the NDI device is the problem" | Unverified. Start at Step 2. | Step 2 |

⚠️ Two traps from past incidents, both in project memory:

- **"Timed out waiting for first frame" is not camera-specific.** A bad or
  shared YouTube stream key produces the identical symptom.
- **If the complaint is "stream won't go live / stuck Preparing,"** check
  YouTube Studio's **Dual stream** toggle before blaming the network. That cost
  us a full debugging session once; it is key- and quality-independent.

---

## Step 2 — Measure, with his device running

```sh
sudo bash scripts/network-triage.sh 30
```

Read in this order:

1. **§1 link health.** A link that negotiated **100 Mb or half-duplex** looks
   *exactly* like congestion and has nothing to do with NDI. Rule it out first —
   it's a bad cable or a bad switch port, and it's a 30-second fix.
2. **§2 throughput.** This is ground truth.
   - RX ≈ 8–25 Mbps, TX ≈ 6.2 Mbps → **the network is fine.** Claim 1 is false;
     the problem is elsewhere. Go to Step 5.
   - RX ≫ 30 Mbps while we stream → someone else's video is arriving at this
     box. On a switch that means multicast/flood. Go to Step 3.
   - Non-zero drops/errors in §1 with normal throughput → hardware, not load.
3. **§3 census.** Write down every source. Classify with the table above.
4. **§4 multicast.** Any meaningful packet count in `239.0.0.0/8` is the
   smoking gun.

> **Read the script header's limitation.** On a switched LAN this Pi *cannot*
> see unicast traffic between two other machines. So if his NDI video is visible
> from the Pi at all, it is being flooded — that observation alone is the
> diagnosis. Conversely, invisible ≠ absent; it may be a unicast flow to the
> camera that you can only see from the camera's UI or the switch.

---

## Step 3 — The A/B test (the only thing that proves causation)

Three minutes, and it converts an argument into a measurement:

1. Run the script (30 s window) with his device **running** → `before.txt`
2. Ask him to **quit the NDI application** (not just stop the stream — quit it)
3. Run it again → `after.txt`
4. `diff <(grep Mbps before.txt) <(grep Mbps after.txt)`

- **RX drops sharply** → confirmed. His device was the load. Go to Step 6.
- **No change** → his device is *not* the cause, regardless of who said it was.
  Say so plainly, then go back to Step 1 and pick a different branch.

Do it in that order — with-then-without — so he sees the number move. It's much
easier than arguing about it.

---

## Step 4 — If the camera, not the LAN, is the bottleneck

Symptoms: our preview freezes, the stream stutters, but §2 shows a quiet
network. The camera has a small NIC and CPU, and we already put **two**
receivers on it.

Check the camera's own web UI at `http://192.168.111.248/` (digest login) for a
connection/client count. Then:

- Ask him to point his Virtual Input at **lowest / proxy bandwidth** instead of
  full. The camera's proxy stream is ~360p and costs almost nothing. (This is
  the same `bandwidth=lowest` knob we use in `RESOLUTION_DIMS`.)
- Or ask him to receive from a **different source** entirely, so the chapel
  camera stays at two receivers.

---

## Step 5 — If it's actually the uplink, not the LAN

Our egress is only 6.2 Mbps, but it competes with everything else in the
building, and bufferbloat wrecks it long before raw bandwidth does.

- From a Mac on that LAN: `networkQuality` — **Responsiveness should be >300
  RPM.** Under load on a typical shared network it collapses to 50–100, which is
  what produces viewer spinners. This is CLAUDE.md's #1 culprit for a reason.
- §7 of the script pings the YouTube ingest; watch the **standard deviation**,
  not the average. High jitter = bufferbloat.
- Only *then* consider dropping quality `high` → `medium` (6 → 3 Mbps). Be
  honest about what that buys: **it helps only if the uplink is the bottleneck.
  It does nothing for a LAN multicast flood.**

---

## Step 6 — Resolution ladder, cheapest first

Work down this list. Stop at the first one that holds.

| # | Fix | Cost | Durability |
|---|-----|------|------------|
| 1 | He quits the NDI app during meeting hours | Free, instant | Depends on him remembering |
| 2 | **Turn off Multicast Sending** in NDI Access Manager / NDI Tools settings | Free, instant | Good — and it's the correct default |
| 3 | He receives at **lowest/proxy** bandwidth instead of full | Free | Good |
| 4 | He uses a source other than the chapel camera | Free | Good |
| 5 | Enable **IGMP snooping + querier** on the switch | Needs whoever owns church IT | Best network-level fix |
| 6 | Put camera + Pi on their own small switch | ~$20 + a cable | **Most durable** — the critical path never crosses the shared LAN |
| 7 | Drop our quality `high` → `medium` | Free | Only helps uplink contention; not a LAN fix |

Items 5 and 6 are almost certainly a facilities/stake-tech request rather than
something to do live — there's a precedent for that kind of ask in
`facilities-email-north-camera.md`.

### Talking to him

He's a volunteer who's probably solving a real problem — overflow room, a
recording, a second camera angle. Lead with **"help me measure this"** rather
than "you're breaking the network," and ask what he's using it for *before*
proposing anything. If the need is legitimate, the answer is almost always
**"configure it correctly"** (items 2–4), not "stop." Items 2 and 3 usually let
him keep doing exactly what he wants at a fraction of the cost — that's the
outcome to aim for.

---

## Capture the result

If the A/B test confirms or refutes the multicast hypothesis, that's worth
writing down — it'll be the first thing anyone wants next time. Save both
triage outputs, and note the outcome here or in `CONFIG_CHANGELOG.md`.
