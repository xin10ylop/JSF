"""What does JSF alone cost, and will it fit on a smaller box?

Whole-box `uptime` and `free` are useless for sizing when the host also
runs something else: the load average includes the neighbour. This reads
per-unit cgroup accounting, so it reports what the jsf-* units cost and
nothing more -- which is the number that matters when the next host will
be dedicated to them.

CPU is the constraint worth measuring, not RAM. Each bot parses its coin's
full CLOB stream (BTC alone measured 458 messages/second) and runs the
strategy on every book change. Saturating the core does not crash
anything; it adds queueing delay, and the measured edge decays about
0.5c/share for every second of decision lag.

    python3 src/host_budget.py            # 60s sample
    python3 src/host_budget.py --secs 180
"""
import argparse
import glob
import os
import time

CG = "/sys/fs/cgroup"


def units():
    """cgroup dirs for jsf-* services, wherever systemd filed them.

    Template instances (jsf-paperbot@btc.service) live under a generated
    slice, plain units directly under system.slice, so glob for both.
    """
    seen = {}
    for pat in (f"{CG}/system.slice/jsf-*.service",
                f"{CG}/system.slice/*/jsf-*.service"):
        for d in glob.glob(pat):
            seen[os.path.basename(d)] = d
    return dict(sorted(seen.items()))


def read_usec(d):
    try:
        for line in open(os.path.join(d, "cpu.stat")):
            if line.startswith("usage_usec"):
                return int(line.split()[1])
    except OSError:
        pass
    return None


def read_mem(d):
    try:
        return int(open(os.path.join(d, "memory.current")).read().strip())
    except OSError:
        return None


def procs():
    """Fallback for hosts without cgroup v2: our processes, by cmdline.

    Returns {label: (cpu_seconds, rss_bytes)} read from /proc,
    so it works in containers and on cgroup v1 alike. Only matches this
    project's entry points -- a neighbouring bot is never counted.
    """
    out = {}
    page = os.sysconf("SC_PAGE_SIZE")
    tick = os.sysconf("SC_CLK_TCK")
    for d in glob.glob("/proc/[0-9]*"):
        try:
            cmd = open(f"{d}/cmdline", "rb").read().decode(
                "utf-8", "replace").replace("\x00", " ").strip()
        except OSError:
            continue
        # Match the argv of a real python invocation. Substring matching
        # also caught the shell wrapper whose command line merely QUOTES
        # "bot/run.py", which reported a phantom process.
        argv = cmd.split()
        if not argv or "python" not in os.path.basename(argv[0]):
            continue
        if not any(x.endswith(("bot/run.py", "bot/recorder.py"))
                   for x in argv):
            continue
        try:
            st = open(f"{d}/stat").read().rsplit(") ", 1)[1].split()
            cpu_s = (int(st[11]) + int(st[12])) / tick
            # PSS, not RSS: shared interpreter and numpy pages exist once
            # in physical memory but appear in every process's RSS, which
            # overstated this set by 28% and would have cost a whole
            # droplet tier.
            rss = 0
            try:
                for line in open(f"{d}/smaps_rollup"):
                    if line.startswith("Pss:"):
                        rss = int(line.split()[1]) * 1024
                        break
            except OSError:
                pass
            if not rss:
                rss = int(open(f"{d}/statm").read().split()[1]) * page
        except (OSError, IndexError, ValueError):
            continue
        pid = os.path.basename(d)
        label = "recorder" if "recorder" in cmd else (
            cmd.split("--coin")[-1].strip() or "bot")
        out[f"{label} [{pid}]"] = (cpu_s, rss)
    return dict(sorted(out.items()))


def sample_procs(secs):
    p0 = procs()
    if not p0:
        return None
    t0 = time.time()
    time.sleep(secs)
    dt = time.time() - t0
    p1 = procs()
    rows = []
    for k in p1:
        if k not in p0:
            continue
        rows.append((k, (p1[k][0] - p0[k][0]) / dt * 100.0, p1[k][1] / 1e6))
    return rows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--secs", type=float, default=60.0)
    a = ap.parse_args()

    u = units()
    if not u:
        print(f"no jsf-* cgroups under {CG}; falling back to /proc "
              f"(cgroup v1 host or container)\n")
        ncpu = os.cpu_count() or 1
        print(f"host: {ncpu} vCPU")
        rows = sample_procs(a.secs)
        if not rows:
            print("no bot/run.py or bot/recorder.py processes running here.")
            return
        print(f"\n{'process':<32}{'CPU %core':>10}{'PSS MB':>9}")
        tc = tm = 0.0
        for k, pct, mem in rows:
            tc += pct
            tm += mem
            print(f"{k:<32}{pct:>9.1f}%{mem:>9.0f}")
        print(f"{'TOTAL (jsf only)':<32}{tc:>9.1f}%{tm:>9.0f}")
        verdict(tc, tm, n_bots=sum("recorder" not in k for k, _, _ in rows),
                recorder=any("recorder" in k for k, _, _ in rows))
        return
    ncpu = os.cpu_count() or 1
    try:
        total_mb = (os.sysconf("SC_PAGE_SIZE")
                    * os.sysconf("SC_PHYS_PAGES")) / 1e6
    except (ValueError, OSError):
        total_mb = float("nan")
    print(f"host: {ncpu} vCPU, {total_mb:,.0f} MB RAM")
    print(f"sampling {len(u)} jsf units for {a.secs:g}s ...\n")

    t0 = time.time()
    a0 = {k: read_usec(d) for k, d in u.items()}
    time.sleep(a.secs)
    dt = time.time() - t0
    a1 = {k: read_usec(d) for k, d in u.items()}

    print(f"{'unit':<32}{'CPU %core':>10}{'PSS MB':>9}")
    tot_cpu = tot_mem = 0.0
    for k, d in u.items():
        if a0[k] is None or a1[k] is None:
            print(f"{k:<32}{'n/a':>10}{'n/a':>9}")
            continue
        pct = (a1[k] - a0[k]) / 1e6 / dt * 100.0
        mem = (read_mem(d) or 0) / 1e6
        tot_cpu += pct
        tot_mem += mem
        print(f"{k:<32}{pct:>9.1f}%{mem:>9.0f}")
    print(f"{'TOTAL (jsf only)':<32}{tot_cpu:>9.1f}%{tot_mem:>9.0f}")

    verdict(tot_cpu, tot_mem,
            n_bots=sum("paperbot" in k for k in u),
            recorder=any("recorder" in k for k in u))


OS_MB = 150       # sshd, systemd, journald on a stock Ubuntu droplet
EDGECHECK_MB = 300   # the daily jsf-edgecheck job imports pandas + parquet


def verdict(tot_cpu, tot_mem, n_bots=0, recorder=False):
    """Project the FULL system, not the subset that happened to be running.

    Measuring three bots and declaring 1 GB sufficient is how a box ends up
    swapping the day the other two start. Scale to the configured coin set,
    add the recorder if it was not sampled, and leave room for the OS and
    for the daily edge check, which loads pandas and parquet on top of
    everything else.
    """
    try:
        import json as _j
        cfg = _j.load(open(os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "bot", "config.json")))
        n_want = len(cfg.get("coins", ["btc"]))
    except Exception:  # noqa: BLE001
        n_want = n_bots
    if n_bots and n_want > n_bots:
        per_cpu, per_mem = tot_cpu / n_bots, tot_mem / n_bots
        extra = n_want - n_bots
        print(f"\nprojecting {extra} unsampled coin(s) at the per-bot mean "
              f"({per_cpu:.1f}% / {per_mem:.0f} MB)")
        tot_cpu += extra * per_cpu
        tot_mem += extra * per_mem
    if not recorder:
        print("projecting the recorder at 100 MB (not sampled here)")
        tot_mem += 100
    tot_mem += OS_MB
    print(f"\nFULL system on a dedicated box: {tot_cpu / 100:.2f} cores, "
          f"{tot_mem:,.0f} MB steady state "
          f"({tot_mem + EDGECHECK_MB:,.0f} MB during the daily edge check)")
    # Headroom, not averages: these are bursty event-driven loops, and a
    # core that is busy when a signal arrives turns straight into decision
    # lag. Size for the peak, not the mean.
    cpu_tight = tot_cpu >= 60
    ram_tight = tot_mem > 700          # of the ~961 MB a 1 GB droplet gives
    if cpu_tight:
        print("VERDICT: CPU wants 2 vCPU ($18). Saturation does not crash "
              "anything, it adds decision lag, and the edge loses about "
              "0.5c/share per second of it.")
    elif ram_tight:
        print("VERDICT: CPU is fine on 1 vCPU, RAM is not. Take the 2 GB / "
              "1 vCPU tier ($12) — a 1 GB box would sit near the edge and "
              "tip into swap whenever the daily edge check runs.")
    else:
        print("VERDICT: 1 vCPU / 1 GB ($6) is enough, with real headroom.")


if __name__ == "__main__":
    main()
