"""
Telegram voice chat recorder, "always ready" edition (GitHub Actions, public repo).

The second account stays connected all the time and joins a voice chat a few seconds
after it starts. No bot or Cloudflare Worker needed.

  1. connect, start py-tgcalls, then wait (Telegram events + light polling as fallback)
  2. voice chat starts -> join and record right away
  3. voice chat ends -> merge -> mono mp3 -> upload to DEST_CHAT_ID (in background, so a
     new voice chat can be joined while the previous file is still uploading)
  4. GitHub jobs live max 6h: near the end the job starts its successor (other session
     slot A/B, so the same session is never used from two machines at once), the
     successor joins / announces itself in Saved Messages, and this job hands over.

Logs of a public repo are public: nothing identifying is printed here.
"""
import asyncio
import datetime
import json
import os
import subprocess
import sys
import time
import urllib.request
from zoneinfo import ZoneInfo

# ---------------------------------------------------------------- config


def env_int(name, default):
    v = (os.environ.get(name) or "").strip()
    return int(v) if v else default


API_ID = env_int("API_ID", 0)
API_HASH = os.environ.get("API_HASH", "")
SOURCE = env_int("SOURCE_CHAT_ID", 0)
DEST = env_int("DEST_CHAT_ID", 0)
SLOT = (os.environ.get("SLOT") or "A").strip().upper()
SESSION_A = os.environ.get("SESSION_A", "")
SESSION_B = os.environ.get("SESSION_B", "")
IS_HANDOVER = (os.environ.get("IS_HANDOVER") or "") == "1"
RUN_ID = os.environ.get("GITHUB_RUN_ID") or str(int(time.time()))
ADMIN_IDS = {int(x) for x in (os.environ.get("ADMIN_IDS") or "").replace(" ", "").split(",") if x}

HANDOVER_AT = env_int("HANDOVER_AT", 5 * 3600 + 15 * 60)   # start the successor after 5h15m
RUN_SECONDS = env_int("RUN_SECONDS", 5 * 3600 + 40 * 60)   # give up waiting for it after 5h40m
POLL = env_int("POLL", 15)                                 # fallback check interval
WATCHDOG_EVERY = env_int("WATCHDOG_EVERY", 60)
ALONE_TIMEOUT = env_int("ALONE_TIMEOUT", 180)              # stop if nobody else is in the call (0 = off)
BITRATE = os.environ.get("BITRATE") or "64k"
NOTIFY_START = (os.environ.get("NOTIFY_START") or "1") == "1"
TZ = ZoneInfo(os.environ.get("TZ_NAME") or "Asia/Tehran")
REC_DIR = os.path.abspath("rec")
FAILED_DIR = os.path.join(REC_DIR, "failed")
TG_LIMIT = 1950 * 1024 * 1024


def log(*a):
    print(time.strftime("%H:%M:%S"), *a, flush=True)


def err(e):
    """exception type + line in this file only (messages can contain ids/titles)"""
    line, tb = None, e.__traceback__
    while tb is not None:
        if tb.tb_frame.f_code.co_filename == __file__:
            line = tb.tb_lineno
        tb = tb.tb_next
    return f"{type(e).__name__}" + (f" (line {line})" if line else "")


def mask(value):
    """hide a runtime value in GitHub logs if it is ever printed by a library"""
    if value:
        for v in {str(value), str(value).lstrip("-")}:
            print(f"::add-mask::{v}", flush=True)


# ---------------------------------------------------------------- audio helpers


def ffprobe_duration(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                              "-of", "default=nw=1:nk=1", path],
                             capture_output=True, text=True, timeout=120).stdout.strip()
        return float(out)
    except Exception:
        return 0.0


def merge_segments(segments, dst, bitrate=BITRATE):
    segs = [s for s in segments if os.path.exists(s) and os.path.getsize(s) > 2000]
    if not segs:
        log("no usable segments")
        return False
    lst = dst + ".txt"
    with open(lst, "w") as f:
        for s in segs:
            f.write("file '%s'\n" % s.replace("'", "'\\''"))
    r = subprocess.run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                        "-f", "concat", "-safe", "0", "-i", lst,
                        "-vn", "-ac", "1", "-c:a", "libmp3lame", "-b:a", bitrate,
                        "-write_xing", "1", dst])
    if r.returncode != 0 or not os.path.exists(dst) or os.path.getsize(dst) < 2000:
        log("concat failed, trying fallback")
        tmp = []
        for i, s in enumerate(segs):
            t = f"{dst}.{i}.mp3"
            if subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", s, "-vn", "-ac", "1",
                               "-c:a", "libmp3lame", "-b:a", bitrate, t]).returncode == 0:
                tmp.append(t)
        if not tmp:
            return False
        with open(lst, "w") as f:
            for t in tmp:
                f.write("file '%s'\n" % t)
        if subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-f", "concat", "-safe", "0",
                           "-i", lst, "-c", "copy", dst]).returncode != 0:
            return False
    return os.path.exists(dst) and os.path.getsize(dst) > 2000


def split_if_needed(path, limit=TG_LIMIT):
    if os.path.getsize(path) <= limit:
        return [path]
    dur = ffprobe_duration(path) or 1
    n = int(os.path.getsize(path) // limit) + 1
    pattern = path.replace(".mp3", "_%02d.mp3")
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", path, "-f", "segment",
                    "-segment_time", str(int(dur / n) + 1), "-c", "copy", pattern], check=False)
    d = os.path.dirname(path)
    base = os.path.basename(path).replace(".mp3", "_")
    return sorted(os.path.join(d, f) for f in os.listdir(d) if f.startswith(base) and f.endswith(".mp3"))


def fmt_dur(sec):
    sec = int(sec)
    h, m = divmod(sec // 60, 60)
    return f"{h}:{m:02d}:{sec % 60:02d}" if h else f"{m}:{sec % 60:02d}"


def gh_dispatch_successor():
    token = os.environ.get("GH_TOKEN")
    repo = os.environ.get("GITHUB_REPOSITORY")
    ref = os.environ.get("GITHUB_REF_NAME") or "main"
    if not token or not repo:
        log("dispatch skipped: not on GitHub Actions")
        return False
    nxt = "B" if SLOT == "A" else "A"
    body = json.dumps({"ref": ref, "inputs": {"slot": nxt, "handover": "1"}}).encode()
    req = urllib.request.Request(
        f"https://api.github.com/repos/{repo}/actions/workflows/listener.yml/dispatches",
        data=body, method="POST", headers={
            "Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "vc-listener"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            log("successor dispatched, slot", nxt, "status", r.status)
            return True
    except Exception as e:
        log("dispatch error:", err(e))
        return False


# ---------------------------------------------------------------- main


async def main() -> int:
    from telethon import TelegramClient, events, types, utils
    from telethon.sessions import StringSession
    from telethon.tl.functions.channels import GetFullChannelRequest
    from telethon.tl.functions.messages import GetFullChatRequest
    from telethon.tl.functions.phone import GetGroupParticipantsRequest
    from telethon.tl.types import DocumentAttributeAudio
    from pytgcalls import PyTgCalls
    from pytgcalls.types import AudioQuality, ChatUpdate, RecordStream
    try:
        from pytgcalls.types import GroupCallConfig
    except ImportError:
        GroupCallConfig = None

    session = SESSION_B if SLOT == "B" else SESSION_A
    if SLOT == "B" and not SESSION_B:
        log("SESSION_B is missing, falling back to SESSION_A (handover may break the session!)")
        session = SESSION_A
    missing = [k for k, v in {"API_ID": API_ID, "API_HASH": API_HASH, "SESSION_A": session,
                              "SOURCE_CHAT_ID": SOURCE, "DEST_CHAT_ID": DEST}.items() if not v]
    if missing:
        log("missing secrets:", ", ".join(missing))
        return 1
    mask(SOURCE)
    mask(DEST)

    client = TelegramClient(StringSession(session), API_ID, API_HASH,
                            connection_retries=None, retry_delay=3, auto_reconnect=True)
    await client.connect()
    if not await client.is_user_authorized():
        log(f"session {SLOT} is invalid or expired, make a new one")
        return 1
    me = await client.get_me()
    mask(me.id)
    mask(getattr(me, "username", None))
    log("logged in, slot", SLOT, "handover" if IS_HANDOVER else "fresh start")

    async def resolve(peer_id):
        try:
            return await client.get_entity(peer_id)
        except Exception:
            await client.get_dialogs()
            return await client.get_entity(peer_id)

    src = await resolve(SOURCE)
    dst = await resolve(DEST)
    title = getattr(src, "title", None) or "voice chat"
    mask(title)
    src_bare = utils.resolve_id(SOURCE)[0]

    async def get_call():
        if isinstance(src, types.Channel):
            full = await client(GetFullChannelRequest(src))
        else:
            full = await client(GetFullChatRequest(src.id))
        return full.full_chat.call

    async def i_am_in_call(call):
        res = await client(GetGroupParticipantsRequest(
            call=call, ids=[types.InputPeerSelf()], sources=[], offset="", limit=10))
        return any(getattr(p.peer, "user_id", None) == me.id and not getattr(p, "left", False)
                   for p in res.participants)

    async def others_in_call(call):
        res = await client(GetGroupParticipantsRequest(
            call=call, ids=[], sources=[], offset="", limit=20))
        n = sum(1 for p in res.participants
                if getattr(p.peer, "user_id", None) != me.id and not getattr(p, "left", False))
        return max(n, (getattr(res, "count", 0) or 0) - 1)

    async def notify(text):
        try:
            return await client.send_message(dst, text, silent=True)
        except Exception as e:
            log("notify failed:", err(e))

    start = time.time()
    poke = asyncio.Event()             # "something happened, check now"
    kicked = asyncio.Event()
    successor_ready = asyncio.Event()
    state = {"rec": None, "skip_call": None, "force": False, "retry_after": 0.0, "failed_call": None,
             "ignore_kick_until": 0.0}
    uploads = set()
    join_lock = asyncio.Lock()
    os.makedirs(FAILED_DIR, exist_ok=True)

    # ---- fast detection: Telegram pushes these the moment a voice chat starts/ends
    @client.on(events.Raw)
    async def _on_raw(u):
        if isinstance(u, types.UpdateGroupCall):
            cid = getattr(u, "chat_id", None)
            if cid is None or cid == src_bare:
                poke.set()
        elif isinstance(u, (types.UpdateNewChannelMessage, types.UpdateNewMessage)):
            m = u.message
            if isinstance(m, types.MessageService) and isinstance(m.action, types.MessageActionGroupCall):
                try:
                    if utils.get_peer_id(m.peer_id) == SOURCE:
                        poke.set()
                except Exception:
                    pass

    # ---- manual start: /rec in the source group (optionally admins only)
    @client.on(events.NewMessage(chats=SOURCE, pattern=r"^/rec(?:@\w+)?\s*$"))
    async def _on_rec(ev):
        if ADMIN_IDS and ev.sender_id not in ADMIN_IDS:
            return
        log("/rec received")
        state["force"] = True
        state["skip_call"] = None
        state["retry_after"] = 0.0
        poke.set()

    # ---- handover signal from the successor job (Saved Messages of the recorder account)
    @client.on(events.NewMessage(chats="me", pattern=r"^#vcrec ready (\S+)$"))
    async def _on_ready(ev):
        if ev.pattern_match.group(1) == RUN_ID or ev.date.timestamp() < start:
            return
        successor_ready.set()
        poke.set()
        try:
            await ev.delete()
        except Exception:
            pass

    calls = PyTgCalls(client)
    leave_flags = [f for f in (getattr(ChatUpdate.Status, n, None) for n in ("LEFT_CALL", "KICKED"))
                   if f is not None]

    @calls.on_update()
    async def _on_update(_, update):
        if isinstance(update, ChatUpdate) and update.chat_id == SOURCE:
            st = update.status
            if st & ChatUpdate.Status.CLOSED_VOICE_CHAT:
                poke.set()
            elif any(st & f for f in leave_flags):
                if time.time() < state["ignore_kick_until"]:
                    return                      # our own leave while re-joining
                kicked.set()
                poke.set()

    await calls.start()

    join_config = None
    if GroupCallConfig is not None:
        try:
            join_config = GroupCallConfig(auto_start=False)   # never create a voice chat ourselves
        except TypeError:
            join_config = None

    class Rec:
        def __init__(self, call, continued):
            self.call = call
            self.call_id = call.id
            self.started = datetime.datetime.now(TZ)
            self.stamp = self.started.strftime("%Y-%m-%d_%H-%M-%S")
            self.t0 = time.time()
            self.segments = []
            self.status_msg = None
            self.continued = continued
            self.taken_over = False

    async def join_segment(rec, first):
        if not first:
            state["ignore_kick_until"] = time.time() + 8
            try:
                await calls.leave_call(SOURCE)
            except Exception:
                pass
            await asyncio.sleep(2)
        path = os.path.join(REC_DIR, f"seg_{rec.stamp}_{len(rec.segments) + 1:02d}.mp3")
        stream = RecordStream(audio=path, audio_parameters=AudioQuality.HIGH)
        for attempt in range(6):
            try:
                if join_config is not None:
                    await calls.record(SOURCE, stream, config=join_config)
                else:
                    await calls.record(SOURCE, stream)
                rec.segments.append(path)
                kicked.clear()
                log("recording, segment", len(rec.segments))
                return True
            except Exception as e:
                log(f"join attempt {attempt + 1} failed:", err(e))
                try:
                    if await get_call() is None:
                        return False
                except Exception:
                    pass
                await asyncio.sleep(3 if attempt < 2 else 8)
        return False

    async def start_recording(call, continued=False):
        async with join_lock:
            if state["rec"] is not None:
                return
            kicked.clear()
            rec = Rec(call, continued)
            t = time.time()
            if not await join_segment(rec, first=True):
                state["retry_after"] = time.time() + 60
                if state["failed_call"] != call.id:
                    state["failed_call"] = call.id
                    await notify(f"⚠️ نتونستم وارد ویس‌چت «{title}» بشم، یک دقیقه دیگه دوباره تلاش می‌کنم.")
                return
            log(f"joined in {time.time() - t:.1f}s")
            state["rec"] = rec
            if NOTIFY_START:
                rec.status_msg = await notify(f"🔴 در حال ضبط ویس‌چت «{title}»")

    async def finalize(rec):
        await asyncio.sleep(3)
        final = os.path.join(REC_DIR, f"vc_{rec.stamp}.mp3")
        loop = asyncio.get_running_loop()
        ok = await loop.run_in_executor(None, merge_segments, rec.segments, final)
        head = f"🎙 ویس‌چت «{title}»\n📅 {rec.started.strftime('%Y/%m/%d  %H:%M')}"
        if rec.continued:
            head += "\n🧩 ادامه‌ی فایل قبلی"
        if rec.taken_over:
            head += "\n🧩 ادامه در فایل بعدی"
        try:
            if not ok:
                await notify(head + "\n⚠️ ضبط انجام شد ولی فایل صوتی ساخته نشد.")
                return
            duration = ffprobe_duration(final) or (time.time() - rec.t0)
            caption = head + f"\n⏱ {fmt_dur(duration)}"
            if len(rec.segments) > 1:
                caption += f"\n⚠️ وسط ضبط {len(rec.segments) - 1} بار قطع و وصل شد"
            files = split_if_needed(final)
            for i, path in enumerate(files, 1):
                d = int(ffprobe_duration(path)) or int(duration)
                cap = caption + (f"\n📎 فایل {i}/{len(files)}" if len(files) > 1 else "")
                for attempt in range(3):
                    try:
                        await client.send_file(dst, path, caption=cap, attributes=[DocumentAttributeAudio(
                            duration=d, title=f"{title} {rec.started.strftime('%Y/%m/%d %H:%M')}",
                            performer=title)])
                        log("uploaded file", i, "of", len(files))
                        break
                    except Exception as e:
                        log(f"upload attempt {attempt + 1} failed:", err(e))
                        await asyncio.sleep(10 * (attempt + 1))
                else:
                    os.replace(path, os.path.join(FAILED_DIR, f"rec_{rec.stamp}_{i}.mp3"))
                    await notify(caption + "\n⚠️ آپلود ناموفق بود. فایل رمزگذاری‌شده توی Artifacts همین اجرای GitHub هست.")
        except Exception as e:
            log("finalize error:", err(e))
        finally:
            if rec.status_msg is not None:
                try:
                    await rec.status_msg.delete()
                except Exception:
                    pass

    async def finish_recording(leave):
        rec = state["rec"]
        if rec is None:
            return
        state["rec"] = None
        if leave:
            try:
                await calls.leave_call(SOURCE)
            except Exception:
                pass
        log("recording stopped after", fmt_dur(time.time() - rec.t0))
        task = asyncio.create_task(finalize(rec))
        uploads.add(task)
        task.add_done_callback(uploads.discard)

    # ---- startup: if a voice chat is already running, join it now (takes over from the predecessor)
    try:
        call = await get_call()
        if call is not None:
            await start_recording(call, continued=IS_HANDOVER)
    except Exception as e:
        log("startup check error:", err(e))

    async def announce_ready():
        try:
            m = await client.send_message("me", f"#vcrec ready {RUN_ID}", silent=True)
            await asyncio.sleep(120)
            await m.delete()
        except Exception:
            pass

    asyncio.create_task(announce_ready())
    log("listening")

    dispatched = False
    dispatch_tries = 0
    last_dispatch_try = 0.0
    last_check = time.time()
    last_watchdog = time.time()
    absent = 0
    alone_since = None
    rejoin_failures = 0
    write_restart_flag = False

    try:
        while True:
            try:
                await asyncio.wait_for(poke.wait(), timeout=5)
            except asyncio.TimeoutError:
                pass
            poked = poke.is_set()
            poke.clear()
            now = time.time()
            elapsed = now - start

            if successor_ready.is_set():
                log("successor is ready, handing over")
                if state["rec"] is not None:
                    state["rec"].taken_over = True
                    await finish_recording(leave=False)   # leaving would kick the successor out
                break

            if elapsed >= RUN_SECONDS:
                log("run time over and no successor showed up")
                await finish_recording(leave=True)
                write_restart_flag = True
                break

            if (not dispatched and elapsed >= HANDOVER_AT and dispatch_tries < 5
                    and now - last_dispatch_try >= 60):
                last_dispatch_try = now
                dispatch_tries += 1
                dispatched = gh_dispatch_successor()

            rec = state["rec"]
            if poked or now - last_check >= POLL:
                last_check = now
                try:
                    cur = await get_call()
                    ok = True
                except Exception as e:
                    log("check error:", err(e))
                    ok = False
                if ok:
                    if rec is not None and (cur is None or cur.id != rec.call_id):
                        log("voice chat ended")
                        await finish_recording(leave=cur is not None)
                        rec = None
                    if cur is None:
                        state["skip_call"] = None
                        state["force"] = False
                    elif rec is None and now >= state["retry_after"]:
                        if state["skip_call"] == cur.id:
                            try:
                                if await others_in_call(cur) > 0:
                                    state["skip_call"] = None
                            except Exception:
                                pass
                        if state["skip_call"] != cur.id:
                            state["force"] = False
                            await start_recording(cur)
                            if state["rec"] is not None:
                                last_watchdog = time.time()
                                absent = 0
                                alone_since = None
                                rejoin_failures = 0
                            continue

            rec = state["rec"]
            if rec is None or not (kicked.is_set() or now - last_watchdog >= WATCHDOG_EVERY):
                continue

            if kicked.is_set() and dispatched:
                # the successor probably joined the call, give it a moment to announce itself
                try:
                    await asyncio.wait_for(successor_ready.wait(), timeout=20)
                except asyncio.TimeoutError:
                    pass
                if successor_ready.is_set():
                    continue

            last_watchdog = time.time()
            present = True
            try:
                present = await i_am_in_call(rec.call)
            except Exception as e:
                log("watchdog error:", err(e))

            if ALONE_TIMEOUT > 0:
                try:
                    if await others_in_call(rec.call) == 0:
                        alone_since = alone_since or time.time()
                        if time.time() - alone_since >= ALONE_TIMEOUT:
                            log("nobody else in the call, stopping (will re-join if someone comes back)")
                            state["skip_call"] = rec.call_id
                            await finish_recording(leave=True)
                            continue
                    else:
                        alone_since = None
                except Exception as e:
                    log("participants check error:", err(e))

            absent = 0 if present else absent + 1
            if kicked.is_set() or absent >= 2:
                kicked.clear()
                absent = 0
                log("dropped from the call, re-joining")
                if await join_segment(rec, first=False):
                    rejoin_failures = 0
                else:
                    rejoin_failures += 1
                    if rejoin_failures >= 3:
                        log("giving up re-joining")
                        await finish_recording(leave=True)
                        state["retry_after"] = time.time() + 60
    except Exception as e:
        log("main loop error:", err(e))
        await finish_recording(leave=not successor_ready.is_set())
        write_restart_flag = not successor_ready.is_set()
    finally:
        if uploads:
            log("waiting for uploads to finish")
            await asyncio.gather(*list(uploads), return_exceptions=True)
        if write_restart_flag:
            with open("restart.flag", "w") as f:
                f.write("B" if SLOT == "A" else "A")
        await client.disconnect()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(main()))
    except Exception as e:
        log("fatal:", err(e))
        sys.exit(1)
