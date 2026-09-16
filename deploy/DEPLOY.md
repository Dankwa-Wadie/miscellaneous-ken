# Deploying to Oracle Cloud Always Free

Goal: the pipeline runs three times a day without your Mac being involved at all.

**Read this first — the honest caveats.**

1. **YouTube blocks datacenter IPs.** This is the main risk to the whole plan.
   yt-dlp on a cloud server often hits *"Sign in to confirm you're not a bot"*.
   It may work for weeks and then stop. The mitigation is a cookies file
   (below), and `agent.py` now recognises this specific error and says so in
   the alert rather than reporting a generic download failure.
2. **Oracle's ARM capacity is frequently exhausted.** "Out of host capacity"
   when creating an Ampere A1 instance is normal, not a mistake you made.
   Retry, or try a different availability domain. Some people wait days.
3. **Always Free can reclaim idle instances.** This one won't be idle (three
   runs a day), but it isn't a contractual guarantee.

If any of that turns out to be a wall, the fallback is running the same
schedule on a machine at home — the code is identical, only the scheduler
differs.

---

## 0. The Oracle account itself

You need an Oracle Cloud account before any of this. It's free, but the signup
has three decisions that are hard or impossible to undo.

**A credit card is required for identity verification.** Always Free resources
aren't charged, but Oracle places a small temporary authorisation (around $1)
to prove the card is real. Debit cards and virtual cards are often rejected.

**Your home region is permanent.** You choose it during signup and it cannot be
changed afterwards — a new region means a new account. From Ghana, pick a
European region: **UK South (London)** or **Germany Central (Frankfurt)**.
Latency to YouTube, the Anthropic API and Telegram will all be better than a US
region, and it's the difference between a download taking 30 seconds and taking
two minutes.

**"Free Trial" and "Always Free" are different things.** New accounts get $300
of trial credits for 30 days. When those expire the account converts to Always
Free, and *anything not Always Free eligible is stopped and eventually
deleted*. This is the single most common way people lose a server they thought
was free. When creating the instance, the shape must be visibly labelled
**"Always Free-eligible"**. If it isn't, it will die in 30 days.

Sign up at [cloud.oracle.com/free](https://cloud.oracle.com/free). Expect email
and phone verification. Be aware that Oracle sometimes rejects signups with no
clear reason — if that happens it's not something you did wrong, and it's the
point at which running on your own hardware becomes the pragmatic answer.

Once you're in, the console is at cloud.oracle.com. It is not a friendly
interface; the instance creation page in particular buries the important
choices among many you can safely ignore.

## 1. Create the instance

Oracle Cloud Console → **Compute → Instances → Create instance**

- **Image:** Canonical Ubuntu 22.04 (or 24.04)
- **Shape:** Ampere **VM.Standard.A1.Flex** — 2 OCPU / 12 GB is plenty and
  leaves headroom in the free allowance (4 OCPU / 24 GB total).
  Avoid `E2.1.Micro`: 1 GB RAM and a fraction of a core will render very
  slowly, if at all.
- **SSH keys:** on your Mac, `ssh-keygen -t ed25519` if you have no key yet,
  then upload `~/.ssh/id_ed25519.pub` (the `.pub` one — never the other).
  **This is your only way in.** Oracle sets no password; lose the key and the
  instance is unreachable and has to be rebuilt.
- **Networking:** accept the default VCN the wizard offers. It creates a public
  subnet with an internet gateway and allows SSH on port 22, which is
  everything this needs. **You do not need to open any other ports** — the
  agent only makes outbound connections, nothing listens. That's a real
  security advantage over the `runner.py` design, which had to expose 8765.
- **Boot volume:** the 50 GB default is fine (Always Free allows 200 GB total).

Watch for the **"Always Free-eligible"** label on the shape before you click
Create. If it isn't there, change the shape until it is.

Note the public IP when it finishes provisioning.

### If you get "Out of host capacity"

Very common on the ARM shape, and not your fault — Always Free A1 capacity is
heavily oversubscribed. In rough order of effort:

1. Try a different **availability domain** in the same region (AD-1, AD-2, AD-3).
2. Retry periodically; capacity frees up unpredictably.
3. Upgrade the account to **Pay As You Go**. Counter-intuitively this often
   unblocks A1 immediately, and Always Free resources remain free — but you
   then have a live payment method against an account that *can* incur charges
   if you ever exceed the free limits. Only do this if you're comfortable
   watching the billing page.

If none of that works within a day or two, say so and we'll put the same
schedule on your Mac with launchd instead. The agent code is identical; only
the scheduler changes.

```bash
ssh ubuntu@<server-ip>
```

## 2. Copy the project up

From your Mac:

```bash
cd ~/projects
tar --exclude='.venv' --exclude='work' --exclude='out' --exclude='__pycache__' \
    -czf mken.tgz miscellaneous-ken
scp mken.tgz ubuntu@<server-ip>:~
ssh ubuntu@<server-ip> 'tar xzf mken.tgz && rm mken.tgz'
```

Then the two credential files, separately and deliberately:

```bash
cd ~/projects/miscellaneous-ken
scp client_secret.json token.json ubuntu@<server-ip>:~/miscellaneous-ken/
```

`token.json` is the live upload credential for your channel. Treat it like a
password: `chmod 600` (the setup script does this), never commit it, and if
the server is ever compromised, revoke it at
[myaccount.google.com/permissions](https://myaccount.google.com/permissions).

**Do not copy `state.json`** unless you want the server to inherit what the Mac
has already seen. A fresh `state.json` is usually what you want — though it
does mean a video already posted from the Mac could be posted again.

## 3. Run setup

```bash
cd ~/miscellaneous-ken
bash deploy/setup.sh
```

It stops the first time and tells you to fill in `mken.env`:

```bash
nano mken.env      # ANTHROPIC_API_KEY and TELEGRAM_BOT_TOKEN
bash deploy/setup.sh
```

For the Telegram token: message **@BotFather**, `/newbot`, follow the prompts.
Then **message your new bot once** — a bot cannot open a conversation with you,
so until you send it something, `sendMessage` fails with "chat not found".

## 4. Verify, in increasing order of consequence

```bash
cd ~/miscellaneous-ken

./.venv/bin/python3 agent.py --notify-test           # alerting
./.venv/bin/python3 agent.py --check-sources         # channel resolution
./.venv/bin/python3 agent.py --dry-run --limit 1 -v  # THE important one
```

That third command is where a datacenter-IP block would surface. If you see
*"Sign in to confirm you're not a bot"*, jump to Troubleshooting.

Then a real run, still `private` per `config.json`:

```bash
sudo systemctl start mken.service
journalctl -u mken.service -f
```

## 5. Hand over from the Mac

Once a server run has succeeded end to end:

```bash
# On the Mac — stop the old path so nothing double-posts
# (n8n) open the workflow and toggle it inactive
# (runner) Ctrl-C the runner.py terminal
```

Both systems share a YouTube quota (~6 uploads/day) and neither knows about the
other's `state.json`, so leaving both running risks duplicate posts and a
quota wall. Pick one.

## Operating it

```bash
systemctl list-timers mken.timer          # when does it next run
journalctl -u mken.service -n 100         # last run's log
journalctl -u mken.service --since today
sudo systemctl start mken.service         # run now
sudo systemctl stop mken.timer            # pause the schedule
sudo systemctl start mken.timer           # resume
```

Updating after a code change:

```bash
scp agent.py render.py config.json ubuntu@<server-ip>:~/miscellaneous-ken/
# no restart needed — each run starts a fresh process
```

## Troubleshooting

**"Sign in to confirm you're not a bot" / HTTP 403 on download**
The datacenter-IP problem. On your Mac, install a `cookies.txt` browser
extension, export cookies for youtube.com while signed in, then:

```bash
scp cookies.txt ubuntu@<server-ip>:~/miscellaneous-ken/
ssh ubuntu@<server-ip> 'chmod 600 ~/miscellaneous-ken/cookies.txt'
# uncomment YTDLP_COOKIES in mken.env
```

Cookies expire, typically in weeks. When they do, downloads fail again and the
Telegram alert is your notice. Use a throwaway Google account if you'd rather
not have your main account's cookies sitting on a server — a sensible
precaution, since those cookies are effectively a login.

**yt-dlp extraction errors**
Usually an outdated yt-dlp: `./.venv/bin/pip install --upgrade yt-dlp`.

**Renders are slow**
Check the `encoded in Xs` line. ARM has no VideoToolbox, so it falls back to
libx264. If it's too slow, set `"fps": 24` and `"max_clip_seconds": 20` in
`config.json` — together roughly halves the work.

**Nothing runs**
`systemctl list-timers mken.timer`. If absent, `sudo systemctl enable --now
mken.timer`. If the service fails instantly, `journalctl -u mken.service -n 50`
— an `EnvironmentFile` typo (quotes around values, or a stray `export`) is the
usual cause.

**Uploads fail with insufficient permissions**
`token.json` didn't copy correctly, or was authorised for the wrong channel.
Re-run `youtube_auth.py` on the Mac and copy the new token up.
